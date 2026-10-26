from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from apps.workflows.models.engine import ImmutableVersionPart


class HRPolicyCategory(BaseModel):
    company = models.ForeignKey("tenants.Company", null=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=30, default="policy")
    code = models.SlugField(max_length=80)
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "kind", "code"], name="hr_company_category"
            ),
            models.UniqueConstraint(
                fields=["kind", "code"],
                condition=models.Q(company__isnull=True),
                name="hr_system_category",
            ),
        ]


class HRPolicySettings(BaseModel):
    company = models.OneToOneField("tenants.Company", on_delete=models.PROTECT)
    precedence = models.JSONField(default=list)
    revision = models.PositiveIntegerField(default=1)


class HRPolicy(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    department = models.ForeignKey(
        "workspaces.Department", null=True, on_delete=models.PROTECT
    )
    title = models.CharField(max_length=200)
    code = models.SlugField(max_length=80)
    category = models.ForeignKey(HRPolicyCategory, on_delete=models.PROTECT)
    draft = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    review_run = models.ForeignKey(
        "workflows.WorkflowRun", null=True, on_delete=models.PROTECT
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="hr_policy_code")
        ]


class HRPolicyVersion(ImmutableVersionPart):
    policy = models.ForeignKey(
        HRPolicy, on_delete=models.PROTECT, related_name="versions"
    )
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    category = models.ForeignKey(HRPolicyCategory, on_delete=models.PROTECT)
    department = models.ForeignKey(
        "workspaces.Department", null=True, on_delete=models.PROTECT
    )
    document = models.ForeignKey("documents.Document", on_delete=models.PROTECT)
    source_digest = models.CharField(max_length=64)
    governance_revision = models.PositiveIntegerField()
    effective_from = models.DateField()
    effective_until = models.DateField(null=True)
    scope = models.JSONField(default=dict)
    priority = models.IntegerField(default=0)
    supersedes = models.ForeignKey(
        "self", null=True, on_delete=models.PROTECT, related_name="replacements"
    )
    workflow_run = models.OneToOneField(
        "workflows.WorkflowRun", on_delete=models.PROTECT
    )
    coverage = models.JSONField(default=dict)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "number"], name="hr_version_number"
            ),
            models.CheckConstraint(
                check=models.Q(effective_until__isnull=True)
                | models.Q(effective_until__gt=models.F("effective_from")),
                name="hr_effective_interval",
            ),
        ]
        indexes = [models.Index(fields=["category", "effective_from"])]


class HRPolicyPublication(BaseModel):
    version = models.OneToOneField(
        HRPolicyVersion, on_delete=models.PROTECT, related_name="publication"
    )
    status = models.CharField(max_length=20, default="published")
    revision = models.PositiveIntegerField(default=1)
    retired_at = models.DateTimeField(null=True)
    retired_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )


class HRPolicySection(ImmutableVersionPart):
    version = models.ForeignKey(
        HRPolicyVersion, on_delete=models.PROTECT, related_name="sections"
    )
    position = models.PositiveIntegerField()
    heading = models.CharField(max_length=200)
    evidence = models.JSONField(default=dict)
    digest = models.CharField(max_length=64)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "position"], name="hr_section_position"
            )
        ]


class HRPolicyAnswer(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    key = models.CharField(max_length=128)
    fingerprint = models.CharField(max_length=64)
    message = models.OneToOneField("ai_engine.AIMessage", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "key"], name="hr_answer_idempotency"
            )
        ]
