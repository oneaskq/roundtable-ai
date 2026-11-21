import hashlib
import ipaddress
import json
import logging
import secrets
import time
from email.utils import parseaddr
from functools import wraps
from urllib.parse import urlsplit

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout, update_session_auth_hash
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import password_changed, validate_password
from django.contrib.auth.tokens import default_token_generator
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.mail import get_connection as get_mail_connection, send_mail
from django.core.validators import validate_email
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.csrf import ensure_csrf_cookie

from .models import DeviceSession, LoginAttempt, MFADevice, SecurityEvent, User


WINDOW = timezone.timedelta(minutes=15)
logger = logging.getLogger(__name__)


def error(message, status=400, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def api(method, protected=False, allow_pending=False):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method != method:
                response = error("请求方式不受支持。", 405)
                response["Allow"] = method
                return response
            if protected:
                if not request.user.is_authenticated:
                    return error("请先登录。", 401)
                if not allow_pending and mfa_status(request)["required"]:
                    return error("请先完成双重验证。", 403, mfa_required=True)
            if method == "POST":
                try:
                    if len(request.body) > 16000:
                        return error("提交内容过长。", 413)
                    request.payload = json.loads(request.body or b"{}")
                    if not isinstance(request.payload, dict):
                        raise ValueError
                except (ValueError, UnicodeError):
                    return error("提交内容格式无效。")
            try:
                return view(request, *args, **kwargs)
            except ValidationError as exc:
                return error("；".join(exc.messages))
        return wrapped
    return decorate


def string(data, name, limit=1024, required=True):
    value = data.get(name, "")
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValidationError("请检查填写的信息。")
    return value


def email_value(data):
    value = string(data, "email", 254).strip().casefold()
    validate_email(value)
    return value


def ip(request):
    # Only the directly connected peer is trusted; proxy headers need explicit deployment support.
    try:
        return str(ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")))
    except ValueError:
        return None


def event(user, kind, message):
    SecurityEvent.objects.create(user=user, kind=kind, message=message)


def attempt_key(scope, value):
    return scope + ":" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def blocked(key, limit=5):
    return LoginAttempt.objects.filter(key=key, started_at__gt=timezone.now() - WINDOW, count__gte=limit).exists()


def count_attempt(key):
    now = timezone.now()
    LoginAttempt.objects.filter(key=key, started_at__lte=now - WINDOW).delete()
    obj, _ = LoginAttempt.objects.get_or_create(key=key, defaults={"started_at": now, "count": 0})
    LoginAttempt.objects.filter(pk=obj.pk).update(count=F("count") + 1)


def mfa_status(request):
    device = MFADevice.objects.filter(user=request.user).first()
    enabled = bool(device and device.enabled)
    needed = request.user.is_superuser or enabled
    # A stale session flag cannot stand in for an enabled authenticator.
    verified = enabled and bool(request.session.get("mfa_verified", False))
    return {"enabled": enabled, "required": needed and not verified,
            "verified": verified, "setup_required": request.user.is_superuser and not enabled}


def recovery_configuration():
    config = {'enabled': getattr(settings, 'EMAIL_DELIVERY_ENABLED', False), 'backend': settings.EMAIL_BACKEND,
              'host': getattr(settings, 'EMAIL_HOST', ''), 'port': getattr(settings, 'EMAIL_PORT', 587),
              'username': getattr(settings, 'EMAIL_HOST_USER', ''), 'password': getattr(settings, 'EMAIL_HOST_PASSWORD', ''),
              'tls': getattr(settings, 'EMAIL_USE_TLS', False), 'ssl': getattr(settings, 'EMAIL_USE_SSL', False),
              'sender': getattr(settings, 'DEFAULT_FROM_EMAIL', ''),
              'local': getattr(settings, 'LOCAL_EMAIL_RECOVERY_ENABLED', False)}
    if getattr(settings, 'DEBUG', False) and getattr(settings, 'LOCAL_MAIL_CONFIG_ENABLED', False):
        from .local_mail import MailConfigError, load_local_mail
        try:
            local = load_local_mail(settings.LOCAL_EMAIL_PATH, settings.MFA_ENCRYPTION_KEY)
        except MailConfigError:
            local = None
        if local:
            config.update(enabled=True, backend='django.core.mail.backends.smtp.EmailBackend',
                          host=local['host'], port=local['port'], username=local['username'], password=local['password'],
                          tls=local['tls'], ssl=local['ssl'], sender=local['sender'], local=True)
    return config


def recovery_available(config=None):
    """Local/in-memory mail is never presented as an account-recovery service."""
    config = recovery_configuration() if config is None else config
    if not config['enabled']:
        return False
    if config['backend'] != "django.core.mail.backends.smtp.EmailBackend":
        return False
    host = config['host'].strip().casefold()
    sender = parseaddr(config['sender'])[1]
    try:
        validate_email(sender)
        sender_domain = sender.rsplit("@", 1)[1].casefold()
        base = urlsplit(getattr(settings, "PUBLIC_BASE_URL", ""))
        public_host = base.hostname or ""
        local_only = (getattr(settings, 'DEBUG', False) is True and
                      config['local'] is True and
                      base.scheme == 'http' and public_host in ('127.0.0.1', 'localhost') and base.port == 8910)
        domains = (host, sender_domain) if local_only else (host, sender_domain, public_host)
        for domain in domains:
            if not domain or "." not in domain or domain.endswith((".localhost", ".local", ".test", ".invalid")):
                return False
            try:
                if not ipaddress.ip_address(domain).is_global:
                    return False
            except ValueError:
                pass
        if (not local_only and base.scheme != "https") or base.username or base.password or \
                base.path not in ('', '/') or base.query or base.fragment:
            return False
    except (ValidationError, ValueError, IndexError):
        return False
    return bool(config['tls']) != bool(config['ssl'])


def me_payload(request):
    available = recovery_available()
    scope = ('this_computer' if available and urlsplit(settings.PUBLIC_BASE_URL).scheme == 'http' else 'public') if available else None
    if not request.user.is_authenticated:
        return {"authenticated": False, "recovery_available": available, "recovery_scope": scope}
    return {"authenticated": True, "user": {"id": request.user.pk, "email": request.user.email,
            "display_name": request.user.display_name, "role": "admin" if request.user.is_superuser else "user"},
            "mfa": mfa_status(request), "recovery_available": available, "recovery_scope": scope}


def start_session(request, user, verified=False):
    old_key = request.session.session_key
    auth_login(request, user)
    request.session["mfa_verified"] = verified
    if not request.session.session_key:
        request.session.save()
    if old_key and old_key != request.session.session_key:
        DeviceSession.objects.filter(session_key=old_key).update(revoked_at=timezone.now())
    agent = request.META.get("HTTP_USER_AGENT", "")[:300]
    known = DeviceSession.objects.filter(user=user, user_agent=agent).exists()
    DeviceSession.objects.update_or_create(session_key=request.session.session_key, defaults={
        "user": user, "user_agent": agent, "ip_address": ip(request), "revoked_at": None,
        "last_seen_at": timezone.now(),
    })
    event(user, "login" if known else "new_device", "登录成功。" if known else "新设备登录，请在设备会话中核对。")


@ensure_csrf_cookie
@api("GET")
def me(request):
    return JsonResponse(me_payload(request))


@api("POST")
def register(request):
    throttle = attempt_key("register", ip(request) or "unknown")
    if blocked(throttle, 15):
        return error("注册操作过于频繁，请 15 分钟后再试。", 429)
    count_attempt(throttle)
    email = email_value(request.payload)
    password = string(request.payload, "password")
    display_name = string(request.payload, "display_name", 80, False).strip()
    user = User(email=email, display_name=display_name)
    validate_password(password, user)
    try:
        with transaction.atomic():
            user = User.objects.create_user(email, password, display_name=display_name)
    except IntegrityError:
        return error("此邮箱已注册，请登录或恢复账号。", 409)
    start_session(request, user, verified=False)
    event(user, "registered", "账号注册成功。")
    return JsonResponse(me_payload(request), status=201)


@api("POST")
def login(request):
    email = email_value(request.payload)
    password = string(request.payload, "password")
    email_key = attempt_key("login-email", email)
    ip_key = attempt_key("login-ip", ip(request) or "unknown")
    if blocked(email_key) or blocked(ip_key, 30):
        return error("登录尝试过于频繁，请 15 分钟后再试。", 429)
    user = authenticate(request, email=email, password=password)
    if user is None:
        count_attempt(email_key)
        count_attempt(ip_key)
        return error("邮箱或密码不正确，或账号已停用。", 401)
    LoginAttempt.objects.filter(key=email_key).delete()
    device = MFADevice.objects.filter(user=user, enabled=True).first()
    verified = False
    code = request.payload.get("code")
    if code is not None and device:
        if not isinstance(code, str) or not verify_code(user, code):
            return error("验证码无效、已使用或尝试次数过多。", 401, code="credential_invalid")
        verified = True
    start_session(request, user, verified)
    return JsonResponse(me_payload(request))


@api("POST")
def logout(request):
    if request.user.is_authenticated:
        DeviceSession.objects.filter(user=request.user, session_key=request.session.session_key).update(revoked_at=timezone.now())
        event(request.user, "logout", "已退出当前设备。")
    auth_logout(request)
    return JsonResponse({"ok": True})


@api("POST", protected=True)
def change_password(request):
    if set(request.payload) != {"current_password", "new_password", "confirm_password"}:
        return error("请填写当前密码和两次新密码。")
    current_password = string(request.payload, "current_password")
    new_password = string(request.payload, "new_password")
    confirmation = string(request.payload, "confirm_password")
    if new_password != confirmation:
        return error("两次新密码不一致。")
    key = attempt_key("password-change", str(request.user.pk))
    if blocked(key):
        return error("尝试次数过多，请 15 分钟后再试。", 429)
    with transaction.atomic():
        # Take a write lock even on SQLite, then re-read to reject a concurrent
        # password change instead of accepting a stale credential.
        User.objects.filter(pk=request.user.pk, is_active=True).update(is_active=F("is_active"))
        user = User.objects.select_for_update().filter(pk=request.user.pk, is_active=True).first()
        if not user:
            return error("账号不存在或已停用。", 403)
        if not user.check_password(current_password):
            count_attempt(key)
            return error("当前密码不正确。", 401, code="credential_invalid")
        if current_password == new_password:
            return error("新密码需要与当前密码不同。")
        validate_password(new_password, user)
        old_session_key = request.session.session_key
        user.set_password(new_password)
        user.save(update_fields=["password"])
        others = DeviceSession.objects.filter(user=user, revoked_at__isnull=True).exclude(session_key=old_session_key)
        Session.objects.filter(session_key__in=others.values_list("session_key", flat=True)).delete()
        others.update(revoked_at=timezone.now())
        update_session_auth_hash(request, user)
        if old_session_key and request.session.session_key:
            DeviceSession.objects.filter(user=user, session_key=old_session_key, revoked_at__isnull=True).update(
                session_key=request.session.session_key, last_seen_at=timezone.now())
        LoginAttempt.objects.filter(key=key).delete()
        event(user, "password_changed", "登录密码已更新，其他设备需要重新登录。")
    return JsonResponse({"ok": True, "message": "密码已更新；其他设备需要重新登录。"})


def cipher():
    key = getattr(settings, "MFA_ENCRYPTION_KEY", "")
    if isinstance(key, str):
        key = key.encode("ascii")
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise ValidationError("双重验证服务尚未配置，请联系管理员。")


def totp_step(secret, code):
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    current = int(time.time()) // 30
    otp = pyotp.TOTP(secret)
    for step in (current, current - 1, current + 1):
        if secrets.compare_digest(otp.at(step * 30), code):
            return step
    return None


def verify_code(user, code):
    key = attempt_key("mfa", str(user.pk))
    if blocked(key):
        return False
    count_attempt(key)
    success = False
    with transaction.atomic():
        device = MFADevice.objects.select_for_update().filter(user=user, enabled=True).first()
        if not device:
            return False
        try:
            secret = cipher().decrypt(device.encrypted_secret.encode()).decode()
        except InvalidToken:
            raise ValidationError("双重验证配置无法读取，请联系管理员。")
        step = totp_step(secret, code.strip())
        if step is not None and step > device.last_totp_step:
            success = bool(MFADevice.objects.filter(pk=device.pk, last_totp_step=device.last_totp_step)
                           .update(last_totp_step=step))
        elif step is None:
            # Recovery codes are deliberately slower to verify and consumed atomically.
            candidate = code.strip().upper()
            for hashed in device.recovery_hashes:
                if check_password(candidate, hashed):
                    remaining = [item for item in device.recovery_hashes if item != hashed]
                    success = bool(MFADevice.objects.filter(pk=device.pk, recovery_hashes=device.recovery_hashes)
                                   .update(recovery_hashes=remaining))
                    if success:
                        event(user, "recovery_code_used", "已使用一个单次恢复码。")
                    break
    if success:
        LoginAttempt.objects.filter(key=key).delete()
    return success


@api("POST", protected=True, allow_pending=True)
def mfa_setup(request):
    key = attempt_key("mfa-setup", str(request.user.pk))
    if blocked(key):
        return error("尝试次数过多，请 15 分钟后再试。", 429)
    if not request.user.check_password(string(request.payload, "password")):
        count_attempt(key)
        return error("密码不正确。", 401, code="credential_invalid")
    device, _ = MFADevice.objects.get_or_create(user=request.user)
    if device.enabled:
        return error("账号已绑定双重验证。", 409)
    secret = pyotp.random_base32()
    device.pending_secret = cipher().encrypt(secret.encode()).decode()
    device.pending_started_at = timezone.now()
    device.save(update_fields=["pending_secret", "pending_started_at"])
    return JsonResponse({"secret": secret, "provisioning_uri": pyotp.TOTP(secret).provisioning_uri(
        name=request.user.email, issuer_name="圆桌")})


@api("POST", protected=True, allow_pending=True)
def mfa_enable(request):
    code = string(request.payload, "code", 64).strip()
    key = attempt_key("mfa-enable", str(request.user.pk))
    if blocked(key):
        return error("尝试次数过多，请 15 分钟后再试。", 429)
    count_attempt(key)
    with transaction.atomic():
        device = MFADevice.objects.select_for_update().filter(user=request.user).first()
        if not device or device.enabled or not device.pending_secret or not device.pending_started_at:
            return error("请先开始绑定双重验证。")
        if device.pending_started_at < timezone.now() - timezone.timedelta(minutes=10):
            return error("绑定已过期，请重新开始。")
        try:
            step = totp_step(cipher().decrypt(device.pending_secret.encode()).decode(), code)
        except InvalidToken:
            return error("绑定信息已失效，请重新开始。")
        if step is None:
            return error("验证码不正确。")
        recovery = [secrets.token_hex(6).upper() for _ in range(10)]
        device.encrypted_secret = device.pending_secret
        device.pending_secret = ""
        device.pending_started_at = None
        device.enabled = True
        device.last_totp_step = step
        device.recovery_hashes = [make_password(item) for item in recovery]
        device.save()
    request.session["mfa_verified"] = True
    LoginAttempt.objects.filter(key=key).delete()
    event(request.user, "mfa_enabled", "双重验证已启用；恢复码仅展示一次。")
    # Existing sessions must prove the new factor before using protected features.
    old = DeviceSession.objects.filter(user=request.user, revoked_at__isnull=True).exclude(session_key=request.session.session_key)
    Session.objects.filter(session_key__in=old.values_list("session_key", flat=True)).delete()
    old.update(revoked_at=timezone.now())
    return JsonResponse({"enabled": True, "recovery_codes": recovery})


@api("POST", protected=True, allow_pending=True)
def mfa_verify(request):
    code = string(request.payload, "code", 64)
    if not verify_code(request.user, code):
        return error("验证码无效、已使用或尝试次数过多。", 401, code="credential_invalid")
    request.session["mfa_verified"] = True
    event(request.user, "mfa_verified", "双重验证通过。")
    return JsonResponse(me_payload(request))


@api("POST", protected=True)
def mfa_rotate_recovery(request):
    """Replace recovery codes only with the password and a fresh authenticator code."""
    if set(request.payload) != {"password", "code"}:
        return error("请填写当前密码和验证器验证码。")
    password = string(request.payload, "password")
    code = string(request.payload, "code", 16).strip()
    key = attempt_key("mfa-recovery-rotate", str(request.user.pk))
    if blocked(key):
        return error("尝试次数过多，请 15 分钟后再试。", 429)
    with transaction.atomic():
        User.objects.filter(pk=request.user.pk, is_active=True).update(is_active=F("is_active"))
        user = User.objects.select_for_update().filter(pk=request.user.pk, is_active=True).first()
        device = MFADevice.objects.select_for_update().filter(user=user, enabled=True).first() if user else None
        if not user or not device:
            return error("请先开启验证器。", 403)
        try:
            secret = cipher().decrypt(device.encrypted_secret.encode()).decode()
        except (InvalidToken, UnicodeError):
            raise ValidationError("双重验证配置无法读取，请联系管理员。")
        step = totp_step(secret, code)
        if not user.check_password(password) or step is None or step <= device.last_totp_step:
            count_attempt(key)
            return error("密码或验证码不正确，或验证码已使用。", 401, code="credential_invalid")
        recovery = [secrets.token_hex(6).upper() for _ in range(10)]
        device.last_totp_step = step
        device.recovery_hashes = [make_password(item) for item in recovery]
        device.save(update_fields=["last_totp_step", "recovery_hashes"])
        LoginAttempt.objects.filter(key=key).delete()
        event(user, "recovery_codes_rotated", "恢复码已更换；旧恢复码已失效。")
    return JsonResponse({"recovery_codes": recovery})


@api("GET", protected=True)
def sessions(request):
    rows = DeviceSession.objects.filter(user=request.user).order_by("-last_seen_at")[:50]
    active_keys = set(Session.objects.filter(session_key__in=[row.session_key for row in rows],
                                            expire_date__gt=timezone.now()).values_list("session_key", flat=True))
    return JsonResponse({"sessions": [{"id": str(row.pk), "device": row.user_agent or "未知设备",
        "ip_address": row.ip_address, "created_at": row.created_at.isoformat(),
        "last_seen_at": row.last_seen_at.isoformat(), "current": row.session_key == request.session.session_key,
        "active": row.revoked_at is None and row.session_key in active_keys} for row in rows]})


@api("POST", protected=True)
def revoke_session(request, session_id):
    row = DeviceSession.objects.filter(pk=session_id, user=request.user).first()
    if not row:
        return error("会话不存在。", 404)
    Session.objects.filter(session_key=row.session_key).delete()
    row.revoked_at = timezone.now()
    row.save(update_fields=["revoked_at"])
    current = row.session_key == request.session.session_key
    event(request.user, "session_revoked", "已撤销一个设备会话。")
    if current:
        auth_logout(request)
    return JsonResponse({"ok": True, "current_revoked": current})


@api("GET", protected=True)
def security_events(request):
    return JsonResponse({"events": [{"id": row.pk, "kind": row.kind, "message": row.message,
        "created_at": row.created_at.isoformat()} for row in SecurityEvent.objects.filter(user=request.user)[:100]]})


@api("POST")
def password_reset_request(request):
    config = recovery_configuration()
    if not recovery_available(config):
        return error("邮件服务尚未接入，暂时无法发送账号恢复邮件。", 503)
    email = email_value(request.payload)
    key = attempt_key("password-reset", ip(request) or "unknown")
    if blocked(key):
        return error("恢复请求过于频繁，请稍后再试。", 429)
    count_attempt(key)
    user = User.objects.filter(email=email, is_active=True).first()
    if user:
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        base = getattr(settings, "PUBLIC_BASE_URL", "http://127.0.0.1:8904").rstrip("/")
        # Fragments stay in the browser and are not sent in server access logs.
        url = f"{base}/#reset_uid={uid}&reset_token={token}"
        try:
            local_notice = '\n此链接仅可在安装圆桌的这台电脑上打开。' if base.startswith('http://') else ''
            connection = get_mail_connection(backend=config['backend'], host=config['host'], port=config['port'],
                username=config['username'], password=config['password'], use_tls=config['tls'], use_ssl=config['ssl'],
                timeout=getattr(settings, 'EMAIL_TIMEOUT', 15))
            sent = send_mail("圆桌账号恢复", f"请打开以下链接设置新密码。若非本人操作，请忽略此邮件。{local_notice}\n\n{url}",
                             config['sender'], [user.email], fail_silently=False, connection=connection)
            if sent != 1:
                logger.warning('Password recovery email could not be delivered.')
        except Exception:
            # The response must not reveal whether a particular account exists.
            # Never log provider errors, recipient addresses, tokens or secrets.
            logger.warning('Password recovery email could not be delivered.')
    return JsonResponse({"ok": True, "message": "如果该邮箱对应可用账号，我们会尝试发送恢复邮件；未收到时请检查邮件设置。"})


@api("POST")
def password_reset_confirm(request):
    try:
        uid = force_str(urlsafe_base64_decode(string(request.payload, "uid", 100)))
        user = User.objects.get(pk=uid, is_active=True)
    except (ValueError, TypeError, OverflowError, User.DoesNotExist):
        return error("恢复链接无效或已过期。")
    token = string(request.payload, "token", 200)
    if not default_token_generator.check_token(user, token):
        return error("恢复链接无效或已过期。")
    password = string(request.payload, "password")
    validate_password(password, user)
    previous_password, previous_email, previous_login = user.password, user.email, user.last_login
    user.set_password(password)
    try:
        with transaction.atomic():
            # The password, email and last login are inputs to Django's token.
            # Compare-and-swap makes one token single-use even on SQLite, where
            # select_for_update() alone does not lock a previously read row.
            changed = User.objects.filter(pk=user.pk, is_active=True, password=previous_password,
                                          email=previous_email, last_login=previous_login).update(password=user.password)
            if not changed:
                return error("恢复链接无效或已过期。")
            password_changed(password, user)
            rows = DeviceSession.objects.filter(user=user, revoked_at__isnull=True)
            Session.objects.filter(session_key__in=rows.values_list("session_key", flat=True)).delete()
            rows.update(revoked_at=timezone.now())
            event(user, "password_reset", "密码已重置，全部设备会话已失效；双重验证仍然保留。")
    except OperationalError:
        return error("账号状态正在更新，请稍后重试或重新申请恢复链接。", 409)
    return JsonResponse({"ok": True})
