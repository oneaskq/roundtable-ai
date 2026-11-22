import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class CapabilityEvidence(models.Model):
    """An operator's dated acceptance record; never editable by normal product APIs."""
    capability = models.CharField(max_length=80)
    provider = models.CharField(max_length=32, blank=True, default='')
    connection_fingerprint = models.CharField(max_length=64, blank=True, default='')
    model = models.CharField(max_length=200)
    service_region = models.CharField(max_length=80)
    evidence = models.TextField()
    verified_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    region_approved = models.BooleanField(default=False)
    compliance_approved = models.BooleanField(default=False)
    enabled = models.BooleanField(default=False)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['capability', 'connection_fingerprint'],
                                               name='unique_capability_connection')]


class RequestBudget(models.Model):
    HOURLY_LIMIT = 20
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    period_started_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveIntegerField(default=0)

    @classmethod
    def snapshot(cls, user):
        period = cls.objects.filter(user=user).values('attempts', 'period_started_at').first()
        end = period['period_started_at'] + timezone.timedelta(hours=1) if period else None
        if not end or end <= timezone.now():
            return {'remaining': cls.HOURLY_LIMIT, 'resets_at': None}
        return {'remaining': max(0, cls.HOURLY_LIMIT - period['attempts']), 'resets_at': end.isoformat()}


class TeamRun(models.Model):
    """A bounded text collaboration; ownership is inherited from its task."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey('workspace.Task', on_delete=models.CASCADE, related_name='runs')
    execution_id = models.UUIDField(unique=True, editable=False)
    status = models.CharField(max_length=20, default='running')
    team_revision = models.CharField(max_length=64)
    delivery_mode = models.CharField(max_length=16, default='text')
    workflow = models.CharField(max_length=16, default='fixed')
    background = models.BooleanField(default=False, editable=False)
    client_request_id = models.UUIDField(null=True, unique=True, editable=False)
    request_fingerprint = models.CharField(max_length=64, blank=True, editable=False)
    # Server-created context snapshot. Never a queue of client-supplied prompts or credentials.
    input_context = models.JSONField(default=list, editable=False)
    source_revision = models.CharField(max_length=64, blank=True, editable=False)
    source_snapshot = models.JSONField(default=list, editable=False)
    started_at = models.DateTimeField(null=True, editable=False)
    finished_at = models.DateTimeField(null=True, editable=False)
    error = models.TextField(blank=True)
    # Immutable owner-supplied snapshot; conversation edits only affect future runs.
    acceptance_cases = models.JSONField(default=list, editable=False)
    plan = models.JSONField(default=dict)
    checks = models.JSONField(default=list)
    max_calls = models.PositiveSmallIntegerField(default=5)
    calls_used = models.PositiveSmallIntegerField(default=0)
    discussion_counter = models.PositiveIntegerField(default=0, editable=False)
    # Versioned decision discussion, including the user-confirmed brief and
    # traceable public evidence. Legacy runs keep the empty default.
    decision_state = models.JSONField(default=dict, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']


class TeamInterjection(models.Model):
    """A user message submitted during a single team run."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(TeamRun, on_delete=models.CASCADE, related_name='interjections')
    client_request_id = models.UUIDField(unique=True)
    content = models.TextField()
    target_provider = models.CharField(max_length=32, blank=True, default='')
    reply_to_step = models.ForeignKey('AgentStep', null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name='+')
    status = models.CharField(max_length=20, default='pending')
    public_order = models.PositiveIntegerField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']


class AgentStep(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(TeamRun, on_delete=models.CASCADE, related_name='steps')
    position = models.PositiveSmallIntegerField()
    role = models.CharField(max_length=30)
    label = models.CharField(max_length=80)
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=200)
    status = models.CharField(max_length=20, default='pending')
    public_order = models.PositiveIntegerField(null=True, editable=False)
    output = models.TextField(blank=True)
    error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position']
        constraints = [models.UniqueConstraint(fields=['run', 'role'], name='unique_run_role')]


class QualityRound(models.Model):
    """An owner's manual comparison of answers to one frozen synthetic case."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='quality_rounds')
    case_id = models.CharField(max_length=40)
    case_revision = models.CharField(max_length=64)
    case_snapshot = models.JSONField(default=dict, editable=False)
    status = models.CharField(max_length=16, default='collecting')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']


class QualitySample(models.Model):
    """User-imported output: source and provider identity are not verified."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    round = models.ForeignKey(QualityRound, on_delete=models.CASCADE, related_name='samples')
    position = models.PositiveSmallIntegerField()
    model_label = models.CharField(max_length=80)
    output = models.TextField(max_length=12000)
    output_sha256 = models.CharField(max_length=64)
    checks = models.JSONField(default=list, editable=False)
    review = models.JSONField(default=dict)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position']
        constraints = [models.UniqueConstraint(fields=['round', 'position'], name='quality_round_position_unique')]

