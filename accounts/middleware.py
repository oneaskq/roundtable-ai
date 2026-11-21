from django.http import JsonResponse
from django.utils import timezone

from .models import DeviceSession, MFADevice


class MFARequiredMiddleware:
    """Password-authenticated sessions stay constrained until MFA succeeds."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            enabled = MFADevice.objects.filter(user=request.user, enabled=True).exists()
            required = request.user.is_superuser or enabled
            request.mfa_required = required and not (enabled and request.session.get("mfa_verified", False))
            if request.mfa_required and not request.path.startswith("/api/auth/"):
                if request.path.startswith("/api/") or request.path.startswith("/admin/"):
                    return JsonResponse({"error": "请先完成双重验证。", "mfa_required": True}, status=403)
            if request.session.session_key:
                DeviceSession.objects.filter(
                    user=request.user, session_key=request.session.session_key, revoked_at__isnull=True,
                    last_seen_at__lt=timezone.now() - timezone.timedelta(minutes=5),
                ).update(last_seen_at=timezone.now())
        return self.get_response(request)
