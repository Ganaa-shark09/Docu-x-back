from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from .engine import ImmutableVersionPart


class ImmutableComplianceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        from django.core.exceptions import ValidationError

        raise ValidationError("Compliance history is immutable.")

    def delete(self):
        from django.core.exceptions import ValidationError

        raise ValidationError("Compliance history is immutable.")

    def bulk_update(self, *args, **kwargs):
        from django.core.exceptions import ValidationError

        raise ValidationError("Compliance history is immutable.")


class ImmutableCompliancePart(ImmutableVersionPart):
    objects = ImmutableComplianceQuerySet.as_manager()

    class Meta:
        abstract = True


class ComplianceFramework(BaseModel):
    company = models.ForeignKey("tenants.Company", null=True, on_delete=models.PROTECT)
    code = models.SlugField(max_length=80)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    source = models.CharField(max_length=500, blank=True)
    jurisdiction = models.CharField(max_length=100, blank=True)
    draft = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    active_version = models.ForeignKey(
        "ComplianceFrameworkVersion",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="compliance_framework_code"
            ),
            models.UniqueConstraint(
                fields=["code"],
                condition=models.Q(company__isnull=True),
                name="compliance_system_framework_code",
            ),
        ]


class ComplianceFrameworkVersion(ImmutableCompliancePart):
    framework = models.ForeignKey(
        ComplianceFramework, on_delete=models.PROTECT, related_name="versions"
    )
    number = models.PositiveIntegerField()
    metadata = models.JSONField()
    digest = models.CharField(max_length=64)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["framework", "number"], name="compliance_framework_version"
            )
        ]


class ComplianceRequirement(ImmutableCompliancePart):
    version = models.ForeignKey(
        ComplianceFrameworkVersion,
        on_delete=models.PROTECT,
        related_name="requirements",
    )
    parent = models.ForeignKey(
        "self", null=True, on_delete=models.PROTECT, related_name="children"
    )
    code = models.SlugField(max_length=80)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=80)
    requirement_type = models.CharField(max_length=30)
    obligation = models.CharField(max_length=20)
    applicability = models.JSONField(default=dict)
    source = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["version", "code"], name="compliance_requirement_code"
            )
        ]


class ComplianceControl(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    department = models.ForeignKey(
        "workspaces.Department", null=True, on_delete=models.PROTECT
    )
    code = models.SlugField(max_length=80)
    name = models.CharField(max_length=200)
    definition = models.JSONField()
    revision = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="compliance_control_code"
            )
        ]


class ComplianceControlMapping(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    requirement = models.ForeignKey(
        ComplianceRequirement, on_delete=models.PROTECT, related_name="mappings"
    )
    control = models.ForeignKey(
        ComplianceControl, on_delete=models.PROTECT, related_name="mappings"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["requirement", "control"], name="compliance_requirement_control"
            )
        ]


class ComplianceAssessment(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    department = models.ForeignKey(
        "workspaces.Department", null=True, on_delete=models.PROTECT
    )
    title = models.CharField(max_length=200)
    framework_version = models.ForeignKey(
        ComplianceFrameworkVersion, on_delete=models.PROTECT
    )
    scope = models.JSONField()
    snapshot = models.JSONField()
    revision = models.PositiveIntegerField(default=1)
    evidence_revision = models.PositiveIntegerField(default=1)
    run = models.OneToOneField(
        "workflows.WorkflowRun", null=True, on_delete=models.PROTECT
    )
    previous = models.ForeignKey(
        "self", null=True, on_delete=models.PROTECT, related_name="reassessments"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=128)
    command_digest = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "idempotency_key"],
                name="compliance_assessment_command",
            )
        ]
        indexes = [models.Index(fields=["company", "department"])]


class ComplianceEvidence(ImmutableCompliancePart):
    assessment = models.ForeignKey(
        ComplianceAssessment, on_delete=models.PROTECT, related_name="evidence"
    )
    requirement = models.ForeignKey(ComplianceRequirement, on_delete=models.PROTECT)
    control = models.ForeignKey(ComplianceControl, on_delete=models.PROTECT)
    source_kind = models.CharField(max_length=30)
    source_uuid = models.UUIDField()
    pin = models.JSONField()
    dates = models.JSONField(default=dict)
    location = models.JSONField(default=dict)
    externally_shareable = models.BooleanField(default=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=128)
    command_digest = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["assessment", "idempotency_key"],
                name="compliance_evidence_command",
            )
        ]


class ComplianceEvaluation(ImmutableCompliancePart):
    assessment = models.ForeignKey(
        ComplianceAssessment, on_delete=models.PROTECT, related_name="evaluations"
    )
    run = models.ForeignKey("workflows.WorkflowRun", on_delete=models.PROTECT)
    evidence_revision = models.PositiveIntegerField()
    fingerprint = models.CharField(max_length=64)
    data = models.JSONField()
    idempotency_key = models.CharField(max_length=128)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["assessment", "idempotency_key"],
                name="compliance_evaluation_command",
            )
        ]


class ComplianceDecision(ImmutableCompliancePart):
    assessment = models.ForeignKey(
        ComplianceAssessment, on_delete=models.PROTECT, related_name="decisions"
    )
    evaluation = models.ForeignKey(
        ComplianceEvaluation, null=True, on_delete=models.PROTECT
    )
    kind = models.CharField(max_length=30)
    target = models.CharField(max_length=128)
    outcome = models.CharField(max_length=40)
    reason = models.CharField(max_length=2000)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    data = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=128)
    command_digest = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["assessment", "idempotency_key"],
                name="compliance_decision_command",
            )
        ]
        indexes = [models.Index(fields=["assessment", "kind", "target"])]


class ComplianceOutput(ImmutableCompliancePart):
    assessment = models.ForeignKey(
        ComplianceAssessment, on_delete=models.PROTECT, related_name="outputs"
    )
    run = models.OneToOneField("workflows.WorkflowRun", on_delete=models.PROTECT)
    evaluation = models.ForeignKey(ComplianceEvaluation, on_delete=models.PROTECT)
    data = models.JSONField()
    digest = models.CharField(max_length=64)
