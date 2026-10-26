from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from apps.workflows.models.engine import ImmutableVersionPart


class ClauseType(BaseModel):
    company = models.ForeignKey("tenants.Company", null=True, on_delete=models.PROTECT)
    code = models.SlugField(max_length=80)
    name = models.CharField(max_length=150)
    category = models.SlugField(max_length=80)
    description = models.CharField(max_length=2000, blank=True)
    aliases = models.JSONField(default=list)
    document_types = models.JSONField(default=list)
    fact_schema = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="company_clause_code"
            ),
            models.UniqueConstraint(
                fields=["code"],
                condition=models.Q(company__isnull=True),
                name="system_clause_code",
            ),
        ]


class ClausePlaybook(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    department = models.ForeignKey(
        "workspaces.Department", null=True, on_delete=models.PROTECT
    )
    name = models.CharField(max_length=150)
    description = models.CharField(max_length=2000, blank=True)
    draft = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=False)
    active_version = models.ForeignKey(
        "workflows.ClausePlaybookVersion",
        null=True,
        on_delete=models.PROTECT,
        related_name="active_playbooks",
    )

    class Meta:
        indexes = [models.Index(fields=["company", "department", "is_active"])]


class ClausePlaybookVersion(ImmutableVersionPart):
    playbook = models.ForeignKey(
        ClausePlaybook, on_delete=models.PROTECT, related_name="versions"
    )
    number = models.PositiveIntegerField()
    rules = models.JSONField(default=list)
    taxonomy = models.JSONField(default=dict)
    document_types = models.JSONField(default=list)
    effective_from = models.DateField()
    effective_until = models.DateField(null=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["playbook", "number"], name="clause_playbook_version"
            )
        ]


class ClauseAnalysis(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    workflow_run = models.OneToOneField(
        "workflows.WorkflowRun",
        on_delete=models.PROTECT,
        related_name="clause_analysis",
    )
    document = models.ForeignKey(
        "documents.Document", on_delete=models.PROTECT, related_name="clause_analyses"
    )
    contract_review = models.ForeignKey(
        "workflows.ContractReview",
        null=True,
        on_delete=models.PROTECT,
        related_name="clause_analyses",
    )
    playbook_version = models.ForeignKey(
        ClausePlaybookVersion, on_delete=models.PROTECT
    )
    revision = models.PositiveIntegerField(default=1)
    source_digest = models.CharField(max_length=64)
    context = models.JSONField(default=dict)
    coverage = models.JSONField(default=dict)
    analyzed_at = models.DateTimeField(null=True)
    finalized_at = models.DateTimeField(null=True)
    previous_analysis = models.ForeignKey("self", null=True, on_delete=models.PROTECT)

    class Meta:
        indexes = [models.Index(fields=["company", "document"])]


class ClauseInstance(ImmutableVersionPart):
    analysis = models.ForeignKey(
        ClauseAnalysis, on_delete=models.PROTECT, related_name="clauses"
    )
    clause_type = models.ForeignKey(ClauseType, on_delete=models.PROTECT)
    identity = models.CharField(max_length=64)
    heading = models.CharField(max_length=500, blank=True)
    facts = models.JSONField(default=dict)
    evidence = models.JSONField(default=list)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True)
    detection_method = models.CharField(max_length=80)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["analysis", "identity"], name="clause_detection_identity"
            )
        ]


class ClauseCorrection(ImmutableVersionPart):
    clause = models.ForeignKey(
        ClauseInstance, on_delete=models.PROTECT, related_name="corrections"
    )
    revision = models.PositiveIntegerField()
    clause_type = models.ForeignKey(ClauseType, on_delete=models.PROTECT)
    facts = models.JSONField(default=dict)
    evidence = models.JSONField(default=list)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    reason = models.CharField(max_length=4000)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["clause", "revision"], name="clause_correction_revision"
            )
        ]


class ClauseDeviation(ImmutableVersionPart):
    analysis = models.ForeignKey(
        ClauseAnalysis, on_delete=models.PROTECT, related_name="deviations"
    )
    clause = models.ForeignKey(ClauseInstance, null=True, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    identity = models.CharField(max_length=150)
    rule_code = models.SlugField(max_length=80)
    comparison = models.CharField(max_length=40)
    severity = models.CharField(max_length=20)
    observed = models.JSONField(default=dict)
    expected = models.JSONField(default=dict)
    evidence = models.JSONField(default=list)
    requires_approval = models.BooleanField(default=False)
    finding = models.OneToOneField(
        "workflows.WorkflowFinding", null=True, on_delete=models.PROTECT
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["analysis", "revision", "identity"],
                name="clause_deviation_identity",
            )
        ]


class ClauseReviewDecision(ImmutableVersionPart):
    analysis = models.ForeignKey(
        ClauseAnalysis, on_delete=models.PROTECT, related_name="decisions"
    )
    revision = models.PositiveIntegerField()
    target = models.UUIDField()
    target_type = models.CharField(max_length=20)
    decision = models.CharField(max_length=40)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    reason = models.CharField(max_length=4000)
    idempotency_key = models.CharField(max_length=128)
    fingerprint = models.CharField(max_length=64)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["analysis", "idempotency_key"], name="clause_review_idempotency"
            )
        ]


class ClauseAnalysisOutput(ImmutableVersionPart):
    analysis = models.ForeignKey(
        ClauseAnalysis, on_delete=models.PROTECT, related_name="outputs"
    )
    revision = models.PositiveIntegerField()
    data = models.JSONField()
    digest = models.CharField(max_length=64)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["analysis", "revision"], name="clause_output_revision"
            )
        ]
