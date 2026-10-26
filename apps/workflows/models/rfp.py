from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from apps.workflows.models.engine import ImmutableVersionPart


class RFPProject(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    department = models.ForeignKey(
        "workspaces.Department", null=True, on_delete=models.PROTECT
    )
    title = models.CharField(max_length=200)
    revision = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=30, default="ingesting")
    metadata = models.JSONField(default=dict)
    extraction_reviewed = models.BooleanField(default=False)
    run = models.OneToOneField(
        "workflows.WorkflowRun", null=True, on_delete=models.PROTECT
    )
    configuration = models.JSONField(default=dict)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        indexes = [models.Index(fields=["company", "department", "status"])]


class RFPSource(ImmutableVersionPart):
    project = models.ForeignKey(
        RFPProject, on_delete=models.PROTECT, related_name="sources"
    )
    document = models.ForeignKey("documents.Document", on_delete=models.PROTECT)
    role = models.CharField(max_length=40)
    source_digest = models.CharField(max_length=64)
    governance_revision = models.PositiveIntegerField()
    extraction = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "document"], name="rfp_source_document"
            )
        ]


class RFPItem(BaseModel):
    project = models.ForeignKey(
        RFPProject, on_delete=models.PROTECT, related_name="items"
    )
    source = models.ForeignKey(RFPSource, on_delete=models.PROTECT)
    key = models.CharField(max_length=24)
    section = models.CharField(max_length=200)
    kind = models.CharField(max_length=30)
    category = models.SlugField(max_length=80, default="unclassified")
    text = models.TextField()
    mandatory = models.BooleanField(default=False)
    response_required = models.BooleanField(default=True)
    evidence = models.JSONField(default=dict)
    structure = models.JSONField(default=dict)
    assignment = models.JSONField(default=dict)
    compliance = models.CharField(max_length=30, default="unanswered")
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "key"], name="rfp_item_stable_key"
            )
        ]
        indexes = [models.Index(fields=["project", "section"])]


class RFPResponseVersion(ImmutableVersionPart):
    item = models.ForeignKey(
        RFPItem, on_delete=models.PROTECT, related_name="responses"
    )
    number = models.PositiveIntegerField()
    text = models.TextField(blank=True)
    origin = models.CharField(max_length=30)
    grounding = models.CharField(max_length=30, default="insufficient_evidence")
    evidence = models.JSONField(default=list)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=500, blank=True)
    externally_shareable = models.BooleanField(default=False)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "number"], name="rfp_response_version"
            )
        ]


class RFPAttachment(BaseModel):
    project = models.ForeignKey(
        RFPProject, on_delete=models.PROTECT, related_name="attachments"
    )
    source = models.ForeignKey(RFPSource, on_delete=models.PROTECT)
    key = models.CharField(max_length=64)
    text = models.TextField()
    mandatory = models.BooleanField(default=False)
    evidence = models.JSONField(default=dict)
    document = models.ForeignKey(
        "documents.Document", null=True, on_delete=models.PROTECT
    )
    assignment = models.JSONField(default=dict)
    reviewed = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "key"], name="rfp_attachment_key"
            )
        ]


class RFPProposalOutput(ImmutableVersionPart):
    project = models.ForeignKey(
        RFPProject, on_delete=models.PROTECT, related_name="outputs"
    )
    number = models.PositiveIntegerField()
    project_revision = models.PositiveIntegerField()
    run = models.OneToOneField("workflows.WorkflowRun", on_delete=models.PROTECT)
    data = models.JSONField(default=dict)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "number"], name="rfp_output_version"
            )
        ]


class RFPExtractionCorrection(ImmutableVersionPart):
    project = models.ForeignKey(
        RFPProject, on_delete=models.PROTECT, related_name="corrections"
    )
    source = models.ForeignKey(RFPSource, on_delete=models.PROTECT)
    kind = models.CharField(max_length=30)
    key = models.CharField(max_length=80)
    number = models.PositiveIntegerField()
    value = models.JSONField(default=dict)
    evidence = models.JSONField(default=dict)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=500)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "source", "kind", "key", "number"],
                name="rfp_correction_version",
            )
        ]
