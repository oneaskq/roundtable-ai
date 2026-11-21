import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("请输入邮箱。")
        email = email.strip().casefold()
        extra_fields.setdefault("username", uuid.uuid4().hex)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if not extra_fields["is_staff"] or not extra_fields["is_superuser"]:
            raise ValueError("超级管理员必须拥有管理权限。")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=80, blank=True)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="accounts_email_case_insensitive")]

    def save(self, *args, **kwargs):
        self.email = self.email.strip().casefold()
        if not self.username:
            self.username = uuid.uuid4().hex
        return super().save(*args, **kwargs)


class MFADevice(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="mfa_device")
    encrypted_secret = models.TextField(blank=True)
    pending_secret = models.TextField(blank=True)
    pending_started_at = models.DateTimeField(null=True, blank=True)
    enabled = models.BooleanField(default=False)
    last_totp_step = models.BigIntegerField(default=-1)
    recovery_hashes = models.JSONField(default=list)


class DeviceSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="device_sessions")
    session_key = models.CharField(max_length=40, unique=True)
    user_agent = models.CharField(max_length=300, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)


class SecurityEvent(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="security_events")
    kind = models.CharField(max_length=50)
    message = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class LoginAttempt(models.Model):
    key = models.CharField(max_length=100, primary_key=True)
    count = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField()
