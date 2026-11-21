import json
import threading
from unittest.mock import patch

import pyotp
from cryptography.fernet import Fernet
from django.contrib.auth.tokens import default_token_generator
from django.contrib.sessions.models import Session
from django.db import IntegrityError, close_old_connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import DeviceSession, LoginAttempt, MFADevice, SecurityEvent, User


PASSWORD = "A-long-local-test-passphrase-9837!"
TEST_KEY = Fernet.generate_key().decode()


@override_settings(ROOT_URLCONF="accounts.test_urls", MFA_ENCRYPTION_KEY=TEST_KEY,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AccountTests(TestCase):
    def post(self, path, body=None, client=None):
        return (client or self.client).post("/api/auth/" + path, data=json.dumps(body or {}), content_type="application/json")

    def create(self, email="one@example.com", **extra):
        return User.objects.create_user(email=email, password=PASSWORD, **extra)

    def login(self, user=None, client=None):
        user = user or self.create()
        response = self.post("login", {"email": user.email, "password": PASSWORD}, client)
        self.assertEqual(response.status_code, 200)
        return user

    def enable_mfa(self, user=None, client=None):
        user = self.login(user, client)
        setup = self.post("mfa/setup", {"password": PASSWORD}, client).json()
        secret = setup["secret"]
        with patch("accounts.views.time.time", return_value=1700000000):
            response = self.post("mfa/enable", {"code": pyotp.TOTP(secret).at(1700000000)}, client)
        self.assertEqual(response.status_code, 200)
        return user, secret, response.json()["recovery_codes"]

    def test_anonymous_me_sets_csrf_cookie(self):
        response = self.client.get("/api/auth/me")
        self.assertFalse(response.json()["authenticated"])
        self.assertIn("csrftoken", response.cookies)

    def test_registration_normalizes_email_and_ignores_privileges(self):
        response = self.post("register", {"email": "Person@EXAMPLE.COM", "password": PASSWORD,
                                          "display_name": "一问", "is_superuser": True})
        self.assertEqual(response.status_code, 201)
        user = User.objects.get()
        self.assertEqual(user.email, "person@example.com")
        self.assertFalse(user.is_superuser)
        self.assertNotEqual(user.password, PASSWORD)
        self.assertTrue(self.client.get("/api/auth/me").json()["authenticated"])

    @override_settings(AUTH_PASSWORD_VALIDATORS=[{"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}}])
    def test_registration_rejects_weak_password(self):
        self.assertEqual(self.post("register", {"email": "person@example.com", "password": "short"}).status_code, 400)
        self.assertEqual(User.objects.count(), 0)

    def test_email_uniqueness_is_case_insensitive(self):
        self.create("person@example.com")
        response = self.post("register", {"email": "PERSON@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 409)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create("PERSON@EXAMPLE.COM")

    def test_login_uses_normalized_email_and_inactive_account_is_rejected(self):
        user = self.create()
        self.assertEqual(self.post("login", {"email": "ONE@EXAMPLE.COM", "password": PASSWORD}).status_code, 200)
        user.is_active = False
        user.save()
        self.assertEqual(self.post("login", {"email": user.email, "password": PASSWORD}).status_code, 401)

    def test_corrected_login_email_preserves_password_and_owned_data(self):
        from workspace.models import Conversation
        user = self.create('before@example.test')
        conversation = Conversation.objects.create(owner=user, title='Existing work')
        prior_hash = user.password
        user.email = 'after@example.test'
        user.save(update_fields=['email'])
        self.assertEqual(self.post('login', {'email': 'before@example.test', 'password': PASSWORD}).status_code, 401)
        response = self.post('login', {'email': 'after@example.test', 'password': PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['user']['id'], user.pk)
        user.refresh_from_db()
        self.assertEqual(user.password, prior_hash)
        self.assertTrue(Conversation.objects.filter(pk=conversation.pk, owner=user).exists())

    def test_failed_login_limited_by_database_counter_and_expires(self):
        user = self.create()
        for _ in range(5):
            self.assertEqual(self.post("login", {"email": user.email, "password": "incorrect"}).status_code, 401)
        self.assertEqual(self.post("login", {"email": user.email, "password": PASSWORD}).status_code, 429)
        LoginAttempt.objects.update(started_at=timezone.now() - timezone.timedelta(minutes=16))
        self.assertEqual(self.post("login", {"email": user.email, "password": PASSWORD}).status_code, 200)

    def test_real_csrf_enforced_for_registration(self):
        client = Client(enforce_csrf_checks=True)
        body = {"email": "person@example.com", "password": PASSWORD}
        self.assertEqual(self.post("register", body, client).status_code, 403)
        client.get("/api/auth/me")
        response = client.post("/api/auth/register", data=json.dumps(body), content_type="application/json",
                               HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
        self.assertEqual(response.status_code, 201)

    def test_session_metadata_does_not_expose_session_key(self):
        self.login()
        response = self.client.get("/api/auth/sessions")
        rows = response.json()["sessions"]
        self.assertTrue(rows[0]["current"])
        self.assertTrue(rows[0]["active"])
        self.assertNotIn(self.client.session.session_key, response.content.decode())

    def test_revocation_invalidates_other_device(self):
        user = self.login()
        other = Client()
        self.login(user, other)
        row = DeviceSession.objects.get(session_key=other.session.session_key)
        self.assertEqual(self.post(f"sessions/{row.pk}/revoke").status_code, 200)
        self.assertFalse(other.get("/api/auth/me").json()["authenticated"])
        self.assertFalse(Session.objects.filter(session_key=row.session_key).exists())

    def test_cannot_see_or_revoke_another_users_session(self):
        self.login()
        other = Client()
        user = self.create("other@example.com")
        self.login(user, other)
        row = DeviceSession.objects.get(user=user)
        self.assertNotIn(str(row.pk), self.client.get("/api/auth/sessions").content.decode())
        self.assertEqual(self.post(f"sessions/{row.pk}/revoke").status_code, 404)

    def test_current_session_revocation_and_logout(self):
        self.login()
        row = DeviceSession.objects.get(session_key=self.client.session.session_key)
        self.assertTrue(self.post(f"sessions/{row.pk}/revoke").json()["current_revoked"])
        self.assertFalse(self.client.get("/api/auth/me").json()["authenticated"])
        self.assertEqual(self.post("logout").status_code, 200)

    def test_security_events_are_private(self):
        user = self.login()
        other = self.create("other@example.com")
        SecurityEvent.objects.create(user=other, kind="test", message="private-other-event")
        response = self.client.get("/api/auth/security-events")
        self.assertNotIn("private-other-event", response.content.decode())
        self.assertEqual(response.json()["events"][0]["kind"], "new_device")

    def test_superuser_must_enroll_before_private_api_access(self):
        admin = User.objects.create_superuser(email="admin@example.com", password=PASSWORD)
        self.login(admin)
        self.assertTrue(self.client.get("/api/auth/me").json()["mfa"]["setup_required"])
        self.assertEqual(self.client.get("/api/private").status_code, 403)
        self.assertEqual(self.client.get("/api/auth/sessions").status_code, 403)
        self.enable_mfa(admin)
        self.assertEqual(self.client.get("/api/private").status_code, 200)

    def test_promoting_existing_regular_session_requires_mfa(self):
        user = self.login()
        user.is_superuser = True
        user.save()
        self.assertEqual(self.client.get("/api/private").status_code, 403)

    def test_stale_verified_flag_cannot_bypass_missing_admin_authenticator(self):
        admin = User.objects.create_superuser(email="admin@example.com", password=PASSWORD)
        self.login(admin)
        session = self.client.session
        session['mfa_verified'] = True
        session.save()
        self.assertTrue(self.client.get('/api/auth/me').json()['mfa']['setup_required'])
        self.assertTrue(self.client.get('/api/auth/me').json()['mfa']['required'])
        self.assertEqual(self.client.get('/api/private').status_code, 403)
        body = {'current_password': PASSWORD, 'new_password': 'Another-long-test-password-2345!',
                'confirm_password': 'Another-long-test-password-2345!'}
        self.assertEqual(self.post('password/change', body).status_code, 403)

    def test_mfa_setup_requires_password_and_secret_is_encrypted(self):
        user = self.login()
        self.assertEqual(self.post("mfa/setup", {"password": "wrong"}).status_code, 401)
        response = self.post("mfa/setup", {"password": PASSWORD})
        device = MFADevice.objects.get(user=user)
        self.assertNotIn(response.json()["secret"], device.pending_secret)
        self.assertEqual(Fernet(TEST_KEY).decrypt(device.pending_secret.encode()).decode(), response.json()["secret"])

    def test_mfa_setup_expires_and_cannot_enable_with_wrong_code(self):
        user = self.login()
        self.post("mfa/setup", {"password": PASSWORD})
        self.assertEqual(self.post("mfa/enable", {"code": "wrong"}).status_code, 400)
        MFADevice.objects.filter(user=user).update(pending_started_at=timezone.now() - timezone.timedelta(minutes=11))
        self.assertEqual(self.post("mfa/enable", {"code": "123456"}).status_code, 400)
        self.assertFalse(MFADevice.objects.get(user=user).enabled)

    def test_mfa_enrollment_revokes_previous_sessions(self):
        user = self.login()
        other = Client()
        self.login(user, other)
        self.enable_mfa(user)
        self.assertFalse(other.get("/api/auth/me").json()["authenticated"])

    def test_totp_cannot_be_replayed_and_later_code_unlocks(self):
        user, secret, _ = self.enable_mfa()
        self.post("logout")
        self.login(user)
        self.assertEqual(self.client.get("/api/private").status_code, 403)
        with patch("accounts.views.time.time", return_value=1700000000):
            self.assertEqual(self.post("mfa/verify", {"code": pyotp.TOTP(secret).at(1700000000)}).status_code, 401)
        with patch("accounts.views.time.time", return_value=1700000060):
            self.assertEqual(self.post("mfa/verify", {"code": pyotp.TOTP(secret).at(1700000060)}).status_code, 200)
            self.assertEqual(self.post("mfa/verify", {"code": pyotp.TOTP(secret).at(1700000060)}).status_code, 401)
        self.assertEqual(self.client.get("/api/private").status_code, 200)

    def test_recovery_codes_are_hashed_and_single_use(self):
        user, _, codes = self.enable_mfa()
        device = MFADevice.objects.get(user=user)
        self.assertEqual(len(codes), 10)
        self.assertNotIn(codes[0], json.dumps(device.recovery_hashes))
        self.post("logout")
        self.login(user)
        self.assertEqual(self.post("mfa/verify", {"code": codes[0]}).status_code, 200)
        self.assertEqual(self.post("mfa/verify", {"code": codes[0]}).status_code, 401)
        device.refresh_from_db()
        self.assertEqual(len(device.recovery_hashes), 9)

    def test_rotating_recovery_codes_requires_password_and_new_totp(self):
        user, secret, old_codes = self.enable_mfa()
        with patch('accounts.views.time.time', return_value=1700000060):
            code = pyotp.TOTP(secret).at(1700000060)
            wrong = self.post('mfa/recovery/rotate', {'password': 'wrong', 'code': code})
            self.assertEqual(wrong.status_code, 401)
            self.assertEqual(wrong.json()['code'], 'credential_invalid')
            self.assertEqual(self.post('mfa/recovery/rotate', {'password': PASSWORD, 'code': old_codes[0]}).status_code, 401)
            response = self.post('mfa/recovery/rotate', {'password': PASSWORD, 'code': code})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()['recovery_codes']), 10)
            self.assertEqual(self.post('mfa/recovery/rotate', {'password': PASSWORD, 'code': code}).status_code, 401)
        new_codes = response.json()['recovery_codes']
        device = MFADevice.objects.get(user=user)
        self.assertNotIn(new_codes[0], json.dumps(device.recovery_hashes))
        self.post('logout')
        self.login(user)
        self.assertEqual(self.post('mfa/verify', {'code': old_codes[0]}).status_code, 401)
        self.assertEqual(self.post('mfa/verify', {'code': new_codes[0]}).status_code, 200)
        self.assertEqual(self.post('mfa/verify', {'code': new_codes[0]}).status_code, 401)

    def test_rotating_recovery_codes_requires_verified_mfa_and_limits_failures(self):
        user, _, _ = self.enable_mfa()
        self.post('logout')
        self.login(user)
        body = {'password': PASSWORD, 'code': '000000'}
        self.assertEqual(self.post('mfa/recovery/rotate', body).status_code, 403)
        session = self.client.session
        session['mfa_verified'] = True
        session.save()
        for _ in range(5):
            self.assertEqual(self.post('mfa/recovery/rotate', body).status_code, 401)
        self.assertEqual(self.post('mfa/recovery/rotate', body).status_code, 429)

    def test_mfa_failures_are_throttled(self):
        user, _, codes = self.enable_mfa()
        self.post("logout")
        self.login(user)
        for _ in range(5):
            self.assertEqual(self.post("mfa/verify", {"code": "wrong"}).status_code, 401)
        self.assertEqual(self.post("mfa/verify", {"code": codes[0]}).status_code, 401)

    @override_settings(EMAIL_DELIVERY_ENABLED=False)
    def test_recovery_not_claimed_when_email_unconfigured(self):
        self.assertEqual(self.post("password-reset/request", {"email": "person@example.com"}).status_code, 503)

    @override_settings(EMAIL_DELIVERY_ENABLED=True, EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                       EMAIL_HOST="mail.example.com", EMAIL_USE_TLS=True,
                       DEFAULT_FROM_EMAIL="roundtable@example.com", PUBLIC_BASE_URL="https://roundtable.example.com")
    @patch("accounts.views.send_mail", return_value=1)
    def test_recovery_email_contains_token_and_unknown_email_is_neutral(self, send):
        user = self.create()
        response = self.post("password-reset/request", {"email": user.email})
        unknown = self.post("password-reset/request", {"email": "unknown@example.com"})
        self.assertEqual(response.json(), unknown.json())
        send.assert_called_once()
        self.assertIn("reset_token=", send.call_args.args[1])
        self.assertIn("/#reset_uid=", send.call_args.args[1])
        self.assertNotIn(PASSWORD, send.call_args.args[1])

    @override_settings(EMAIL_DELIVERY_ENABLED=True, EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                       EMAIL_HOST="mail.example.com", EMAIL_USE_TLS=True,
                       DEFAULT_FROM_EMAIL="roundtable@example.com", PUBLIC_BASE_URL="https://roundtable.example.com")
    @patch('accounts.views.send_mail', side_effect=OSError('synthetic mail failure'))
    def test_delivery_failure_does_not_reveal_account_existence(self, _send):
        user = self.create()
        with self.assertLogs('accounts.views', level='WARNING'):
            known = self.post('password-reset/request', {'email': user.email})
        unknown = self.post('password-reset/request', {'email': 'unknown@example.com'})
        self.assertEqual((known.status_code, known.json()), (unknown.status_code, unknown.json()))

    @override_settings(EMAIL_DELIVERY_ENABLED=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    @patch("accounts.views.send_mail")
    def test_in_memory_mail_never_claims_recovery_available(self, send):
        self.assertFalse(self.client.get("/api/auth/me").json()["recovery_available"])
        self.assertEqual(self.post("password-reset/request", {"email": "one@example.com"}).status_code, 503)
        send.assert_not_called()

    @override_settings(EMAIL_DELIVERY_ENABLED=True, EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                       EMAIL_HOST="mail.example.com", EMAIL_USE_TLS=True,
                       DEFAULT_FROM_EMAIL="roundtable@localhost", PUBLIC_BASE_URL="https://roundtable.example.com")
    def test_local_sender_never_claims_real_recovery(self):
        self.assertFalse(self.client.get("/api/auth/me").json()["recovery_available"])
        self.assertEqual(self.post("password-reset/request", {"email": "one@example.com"}).status_code, 503)

    def test_password_reset_revokes_sessions_preserves_mfa_and_token_single_use(self):
        user, _, _ = self.enable_mfa()
        user.refresh_from_db()
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        body = {"uid": uid, "token": token, "password": "A-new-long-test-passphrase-4893!"}
        self.assertEqual(self.post("password-reset/confirm", body).status_code, 200)
        self.assertFalse(self.client.get("/api/auth/me").json()["authenticated"])
        self.assertTrue(MFADevice.objects.get(user=user).enabled)
        self.assertEqual(self.post("password-reset/confirm", body).status_code, 400)

    def test_change_password_preserves_current_verified_session_and_revokes_others(self):
        user = self.login()
        other = Client()
        self.login(user, other)
        self.enable_mfa(user)
        # MFA enrollment revoked the first other device; create another session
        # and verify it separately to exercise password-change revocation.
        self.login(user, other)
        with patch('accounts.views.time.time', return_value=1700000060):
            secret = Fernet(TEST_KEY).decrypt(MFADevice.objects.get(user=user).encrypted_secret.encode()).decode()
            self.assertEqual(self.post('mfa/verify', {'code': pyotp.TOTP(secret).at(1700000060)}, other).status_code, 200)
        old_token = default_token_generator.make_token(user)
        new_password = 'Another-long-test-password-2345!'
        response = self.post('password/change', {'current_password': PASSWORD, 'new_password': new_password,
                                                 'confirm_password': new_password})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn(new_password, response.content.decode())
        self.assertTrue(self.client.get('/api/auth/me').json()['authenticated'])
        self.assertFalse(self.client.get('/api/auth/me').json()['mfa']['required'])
        self.assertFalse(other.get('/api/auth/me').json()['authenticated'])
        sessions = self.client.get('/api/auth/sessions').json()['sessions']
        self.assertEqual(sum(bool(item['current'] and item['active']) for item in sessions), 1)
        user.refresh_from_db()
        self.assertFalse(user.check_password(PASSWORD))
        self.assertTrue(user.check_password(new_password))
        self.assertFalse(default_token_generator.check_token(user, old_token))
        self.assertTrue(MFADevice.objects.get(user=user).enabled)

    def test_change_password_rejects_bad_input_and_limits_wrong_current_password(self):
        user = self.login()
        original_hash = user.password
        path = 'password/change'
        body = {'current_password': PASSWORD, 'new_password': 'Another-long-test-password-2345!',
                'confirm_password': 'Another-long-test-password-2345!'}
        self.assertEqual(self.post(path, {**body, 'confirm_password': 'different'}).status_code, 400)
        self.assertEqual(self.post(path, {**body, 'new_password': 'short', 'confirm_password': 'short'}).status_code, 400)
        self.assertEqual(self.post(path, {**body, 'new_password': PASSWORD, 'confirm_password': PASSWORD}).status_code, 400)
        self.assertEqual(self.post(path, {**body, 'user_id': 'other'}).status_code, 400)
        for _ in range(5):
            self.assertEqual(self.post(path, {**body, 'current_password': 'wrong'}).status_code, 401)
        self.assertEqual(self.post(path, body).status_code, 429)
        user.refresh_from_db()
        self.assertEqual(user.password, original_hash)
        LoginAttempt.objects.filter(key__startswith='password-change:').update(started_at=timezone.now() - timezone.timedelta(minutes=16))
        self.assertEqual(self.post(path, body).status_code, 200)

    def test_change_password_requires_login_and_csrf(self):
        body = {'current_password': PASSWORD, 'new_password': 'Another-long-test-password-2345!',
                'confirm_password': 'Another-long-test-password-2345!'}
        self.assertEqual(self.post('password/change', body).status_code, 401)
        user = self.create()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(user)
        self.assertEqual(self.post('password/change', body, csrf_client).status_code, 403)
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))

    def test_invalid_body_and_wrong_method_return_json(self):
        response = self.client.post("/api/auth/login", data="[]", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertEqual(self.client.get("/api/auth/login").status_code, 405)

    @override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.PBKDF2PasswordHasher"])
    def test_production_password_hash_is_pbkdf2(self):
        user = self.create()
        self.assertTrue(user.password.startswith("pbkdf2_sha256$"))
        self.assertTrue(user.check_password(PASSWORD))


@override_settings(ROOT_URLCONF='accounts.test_urls', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ConcurrentRecoveryTests(TransactionTestCase):
    def test_one_token_accepts_at_most_one_simultaneous_password_change(self):
        user = User.objects.create_user(email='recovery-race@example.test', password=PASSWORD)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        original_check = default_token_generator.check_token
        barrier = threading.Barrier(2)
        results = []

        def synchronized_check(*args, **kwargs):
            valid = original_check(*args, **kwargs)
            barrier.wait(timeout=5)
            return valid

        def submit(password):
            close_old_connections()
            try:
                response = Client().post('/api/auth/password-reset/confirm',
                    json.dumps({'uid': uid, 'token': token, 'password': password}), content_type='application/json')
                results.append(response.status_code)
            except Exception as exc:
                results.append(type(exc).__name__)
            finally:
                close_old_connections()

        with patch.object(default_token_generator, 'check_token', side_effect=synchronized_check):
            threads = [threading.Thread(target=submit, args=(password,)) for password in
                       ('Synthetic-new-password-alpha-123!', 'Synthetic-new-password-beta-123!')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results.count(200), 1, results)
        self.assertIn(next(status for status in results if status != 200), (400, 409))
        self.assertEqual(SecurityEvent.objects.filter(user=user, kind='password_reset').count(), 1)
