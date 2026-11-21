import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q


class OwnedRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-updated_at", "-created_at"]


class Project(OwnedRecord):
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True, max_length=4000)

    def __str__(self):
        return self.title


class Conversation(OwnedRecord):
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="conversations")
    title = models.CharField(max_length=120, default="新的任务")
    acceptance_cases = models.JSONField(default=list)

    def clean(self):
        if self.project_id and self.project.owner_id != self.owner_id:
            raise ValidationError({"project": "项目和对话必须属于同一用户。"})


class ComposerDraft(OwnedRecord):
    """An owner's unsent input, including the not-yet-created conversation."""
    conversation = models.ForeignKey(Conversation, null=True, blank=True, on_delete=models.CASCADE,
                                     related_name='composer_drafts')
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL)
    content = models.TextField(blank=True, default='', max_length=12000)
    writer_id = models.UUIDField(null=True, editable=False)
    sequence = models.PositiveBigIntegerField(default=0, editable=False)

    class Meta(OwnedRecord.Meta):
        constraints = [
            models.UniqueConstraint(fields=['owner', 'conversation'], condition=Q(conversation__isnull=False),
                                    name='one_composer_draft_per_conversation'),
            models.UniqueConstraint(fields=['owner'], condition=Q(conversation__isnull=True),
                                    name='one_new_composer_draft_per_owner'),
        ]


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user", "用户"
        ASSISTANT = "assistant", "主助手"
        SYSTEM = "system", "系统"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField(max_length=30000)
    # Encrypted provider continuation; never returned in ordinary messages or exports.
    provider_context = models.TextField(blank=True, default='', editable=False)
    # Editing creates a new branch while keeping the original messages and
    # their replies available in history and exports.
    edited_from = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='edited_versions', editable=False)
    superseded_by = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='superseded_messages', editable=False)
    edit_request_id = models.UUIDField(null=True, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            return super().save(*args, **kwargs)
        using = kwargs.get('using') or self._state.db or 'default'
        with transaction.atomic(using=using):
            # A no-op update acquires SQLite's write lock; on databases with
            # row locks it also serializes writers for this conversation.
            Conversation.objects.using(using).filter(pk=self.conversation_id).update(title=F('title'))
            previous = (Message.objects.using(using).filter(conversation_id=self.conversation_id)
                        .order_by('-created_at').values_list('created_at', flat=True).first())
            result = super().save(*args, **kwargs)
            if previous is not None and self.created_at <= previous:
                self.created_at = previous + timedelta(microseconds=1)
                Message.objects.using(using).filter(pk=self.pk).update(created_at=self.created_at)
            return result

    class Meta:
        ordering = ["created_at", "id"]


class SourceMaterial(OwnedRecord):
    """Immutable extracted text; selection can change only between team runs."""
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="sources")
    kind = models.CharField(max_length=16, choices=[("file", "文件"), ("web", "网页")])
    name = models.CharField(max_length=200)
    media_type = models.CharField(max_length=120)
    content = models.TextField(max_length=12000)
    source_url = models.URLField(max_length=2048, blank=True)
    byte_size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    included = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict)

    class Meta(OwnedRecord.Meta):
        ordering = ["created_at", "id"]

    def clean(self):
        if self.conversation_id and self.conversation.owner_id != self.owner_id:
            raise ValidationError({"conversation": "资料和对话必须属于同一用户。"})


class Memory(OwnedRecord):
    content = models.TextField(max_length=6000)


class Task(OwnedRecord):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        WAITING = "waiting", "待处理"
        RUNNING = "running", "处理中"
        PAUSED = "paused", "已暂停"
        COMPLETED = "completed", "已完成"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="tasks")
    goal = models.TextField(max_length=30000)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    execution_id = models.UUIDField(null=True, blank=True, editable=False)
    superseded_by_message = models.ForeignKey(Message, null=True, blank=True, on_delete=models.SET_NULL,
                                              related_name='superseded_tasks', editable=False)
    edit_message = models.OneToOneField(Message, null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name='edit_task', editable=False)

    class Meta(OwnedRecord.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["conversation"],
                condition=Q(status__in=["draft", "waiting", "running", "paused"]),
                name="one_active_task_per_conversation",
            )
        ]

    def clean(self):
        if self.conversation_id and self.conversation.owner_id != self.owner_id:
            raise ValidationError({"conversation": "任务和对话必须属于同一用户。"})


class TaskEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=40, default="status")
    content = models.TextField(max_length=30000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class Artifact(OwnedRecord):
    class Kind(models.TextChoices):
        TEXT = "text", "文字"
        BUNDLE = "bundle", "多文件成果"

    class ReviewStatus(models.TextChoices):
        READY = "ready", "已通过检查"
        DRAFT = "draft", "待复核"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="artifacts")
    name = models.CharField(max_length=200)
    content = models.TextField(max_length=1000000)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.TEXT)
    files = models.JSONField(default=list)
    validation = models.JSONField(default=dict)
    review_status = models.CharField(max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.READY)
    revision = models.PositiveIntegerField(default=1)

    def clean(self):
        if self.task_id and self.task.owner_id != self.owner_id:
            raise ValidationError({"task": "成果和任务必须属于同一用户。"})


class DataPolicy(models.Model):
    RETENTION_CHOICES = [(0, "手动管理"), (30, "30 天"), (90, "90 天"), (365, "365 天")]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="data_policy")
    retention_days = models.PositiveIntegerField(choices=RETENTION_CHOICES, default=0)
    auto_cleanup = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(retention_days__in=[0, 30, 90, 365]), name="valid_retention_days")]


class DeletionRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待人工处理"
        CANCELLED = "cancelled", "已取消"
        COMPLETED = "completed", "已完成"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="deletion_requests")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    requested_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [models.UniqueConstraint(fields=["user"], condition=Q(status="pending"), name="one_pending_deletion_request")]


class AccountDeletionReceipt(models.Model):
    """Pseudonymous record for unresolved backup processing; no account content."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity_digest = models.CharField(max_length=64, unique=True, editable=False)
    identity_key_id = models.CharField(max_length=64, editable=False)
    deleted_at = models.DateTimeField(auto_now_add=True)
    backup_status = models.CharField(max_length=16, default="pending")


class RecordDeletionReceipt(models.Model):
    """Pseudonymous tombstone for a user-deleted item in older backups."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=24, editable=False)
    object_digest = models.CharField(max_length=64, unique=True, editable=False)
    identity_key_id = models.CharField(max_length=64, editable=False)
    deleted_at = models.DateTimeField(auto_now_add=True)
