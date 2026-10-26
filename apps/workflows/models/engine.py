from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.shared.models import BaseModel


class WorkflowDefinition(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.CASCADE)
    department = models.ForeignKey(
        "workspaces.Department", null=True, blank=True, on_delete=models.PROTECT
    )
    name = models.CharField(max_length=150)
    code = models.SlugField(max_length=80)
    description = models.TextField(blank=True)
    workflow_type = models.CharField(max_length=80, default="generic")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="workflow_company_code"
            )
        ]


class ImmutableVersionPart(BaseModel):
    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError(
                "Published workflow versions are immutable. Publish a new version."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Published workflow versions cannot be deleted.")

    class Meta:
        abstract = True


class WorkflowVersion(ImmutableVersionPart):
    definition = models.ForeignKey(
        WorkflowDefinition, on_delete=models.PROTECT, related_name="versions"
    )
    number = models.PositiveIntegerField()
    entry_step = models.CharField(max_length=80)
    configuration = models.JSONField(default=dict, blank=True)
    allowed_document_types = models.JSONField(default=list, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["definition", "number"], name="workflow_version_number"
            )
        ]
        ordering = ["-number"]


class WorkflowStep(ImmutableVersionPart):
    class Type(models.TextChoices):
        AI = "ai", "AI"
        EXTRACTION = "extraction", "Extraction"
        VALIDATION = "validation", "Validation"
        RULE = "rule", "Rule"
        HUMAN_REVIEW = "human_review", "Human review"
        APPROVAL = "approval", "Approval"
        ASSIGNMENT = "assignment", "Assignment"
        INTEGRATION = "integration", "Integration"
        PROCESSING = "processing", "Internal processing"
        NOTIFICATION = "notification", "Notification"
        TERMINAL = "terminal", "Completion"

    version = models.ForeignKey(
        WorkflowVersion, on_delete=models.PROTECT, related_name="steps"
    )
    key = models.SlugField(max_length=80)
    name = models.CharField(max_length=150)
    step_type = models.CharField(max_length=30, choices=Type.choices)
    configuration = models.JSONField(default=dict, blank=True)
    execution_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["version", "key"], name="workflow_step_key")
        ]


class WorkflowTransition(ImmutableVersionPart):
    version = models.ForeignKey(
        WorkflowVersion, on_delete=models.PROTECT, related_name="transitions"
    )
    source = models.ForeignKey(
        WorkflowStep, on_delete=models.PROTECT, related_name="outgoing"
    )
    target = models.ForeignKey(
        WorkflowStep, on_delete=models.PROTECT, related_name="incoming"
    )
    outcome = models.CharField(max_length=30)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "outcome"], name="workflow_step_outcome"
            )
        ]


class WorkflowRun(BaseModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        REVIEW_REQUIRED = "review_required", "Review required"
        APPROVAL_REQUIRED = "approval_required", "Approval required"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXCEPTION = "exception", "Exception"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    version = models.ForeignKey(
        WorkflowVersion, on_delete=models.PROTECT, related_name="runs"
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    department = models.ForeignKey(
        "workspaces.Department", null=True, on_delete=models.PROTECT
    )
    documents = models.ManyToManyField(
        "documents.Document", through="WorkflowRunDocument"
    )
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.DRAFT
    )
    current_step = models.ForeignKey(WorkflowStep, null=True, on_delete=models.PROTECT)
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    error_type = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=128)
    request_fingerprint = models.CharField(max_length=64)
    correlation_id = models.UUIDField()
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "idempotency_key"], name="workflow_start_idempotency"
            )
        ]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(
                fields=["status", "updated_at"], name="workflow_queue_recovery"
            ),
        ]


class WorkflowRunDocument(BaseModel):
    run = models.ForeignKey(
        WorkflowRun, on_delete=models.CASCADE, related_name="document_links"
    )
    document = models.ForeignKey("documents.Document", on_delete=models.PROTECT)
    purpose = models.CharField(max_length=80, default="input")
    governance_revision = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "document"], name="workflow_run_document"
            )
        ]


class WorkflowStepRun(BaseModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"
        WAITING = "waiting", "Waiting"
        CANCELLED = "cancelled", "Cancelled"

    run = models.ForeignKey(
        WorkflowRun, on_delete=models.CASCADE, related_name="step_runs"
    )
    step = models.ForeignKey(WorkflowStep, on_delete=models.PROTECT)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    attempt = models.PositiveIntegerField(default=0)
    error_type = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    execution_metadata = models.JSONField(default=dict, blank=True)
    approval = models.OneToOneField(
        "approvals.ApprovalRequest",
        null=True,
        on_delete=models.PROTECT,
        related_name="workflow_step_run",
    )
    extraction = models.OneToOneField(
        "workflows.ExtractionRun",
        null=True,
        on_delete=models.PROTECT,
        related_name="workflow_step_run",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "step"], name="workflow_step_execution"
            )
        ]
        indexes = [models.Index(fields=["run", "status"])]


class WorkflowAssignment(BaseModel):
    step_run = models.OneToOneField(
        WorkflowStepRun, on_delete=models.CASCADE, related_name="assignment"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    department = models.ForeignKey(
        "workspaces.Department", null=True, blank=True, on_delete=models.PROTECT
    )
    role = models.CharField(max_length=50, blank=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="workflow_assignments_created",
    )


class WorkflowCommand(BaseModel):
    run = models.ForeignKey(
        WorkflowRun, on_delete=models.CASCADE, related_name="commands"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    key = models.CharField(max_length=128)
    fingerprint = models.CharField(max_length=64)
    action = models.CharField(max_length=30)
    resulting_revision = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "key"], name="workflow_command_idempotency"
            )
        ]


class WorkflowFinding(BaseModel):
    run = models.ForeignKey(
        WorkflowRun, on_delete=models.CASCADE, related_name="findings"
    )
    step_run = models.ForeignKey(WorkflowStepRun, on_delete=models.PROTECT)
    category = models.CharField(max_length=80)
    severity = models.CharField(
        max_length=20,
        choices=[(x, x) for x in ["info", "low", "medium", "high", "critical"]],
    )
    status = models.CharField(
        max_length=20,
        default="open",
        choices=[(x, x) for x in ["open", "accepted", "dismissed", "resolved"]],
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(null=True)
    evidence = models.JSONField(default=list, blank=True)
    review_state = models.CharField(
        max_length=20,
        default="unreviewed",
        choices=[(x, x) for x in ["unreviewed", "reviewed"]],
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT
    )


class WorkflowException(BaseModel):
    run = models.ForeignKey(
        WorkflowRun, on_delete=models.CASCADE, related_name="exceptions"
    )
    step_run = models.ForeignKey(WorkflowStepRun, on_delete=models.PROTECT)
    category = models.CharField(max_length=80)
    status = models.CharField(
        max_length=20,
        default="open",
        choices=[("open", "Open"), ("resolved", "Resolved")],
    )
    safe_message = models.CharField(max_length=255)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="workflow_exceptions",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="workflow_exceptions_resolved",
    )
    resolved_at = models.DateTimeField(null=True)
    resolution = models.TextField(blank=True)


class WorkflowComment(BaseModel):
    run = models.ForeignKey(
        WorkflowRun, on_delete=models.CASCADE, related_name="comments"
    )
    step_run = models.ForeignKey(WorkflowStepRun, null=True, on_delete=models.PROTECT)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    body = models.TextField()


class WorkflowArtifact(BaseModel):
    run = models.ForeignKey(
        WorkflowRun, on_delete=models.CASCADE, related_name="artifacts"
    )
    step_run = models.OneToOneField(
        WorkflowStepRun, on_delete=models.PROTECT, related_name="artifact"
    )
    kind = models.CharField(max_length=80)
    data = models.JSONField(default=dict)
    evidence = models.JSONField(default=list, blank=True)
