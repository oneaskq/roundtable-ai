from django.urls import path

from . import views


urlpatterns = [
    path("me", views.me),
    path("register", views.register),
    path("login", views.login),
    path("logout", views.logout),
    path("password/change", views.change_password),
    path("mfa/setup", views.mfa_setup),
    path("mfa/enable", views.mfa_enable),
    path("mfa/verify", views.mfa_verify),
    path("mfa/recovery/rotate", views.mfa_rotate_recovery),
    path("sessions", views.sessions),
    path("sessions/<uuid:session_id>/revoke", views.revoke_session),
    path("security-events", views.security_events),
    path("password-reset/request", views.password_reset_request),
    path("password-reset/confirm", views.password_reset_confirm),
]
