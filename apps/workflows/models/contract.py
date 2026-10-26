from django.db import models

from apps.shared.models import BaseModel
from apps.workflows.models.engine import ImmutableVersionPart


class ContractReview(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.CASCADE)
    workflow_run = models.OneToOneField(
        "workflows.WorkflowRun",
        on_delete=models.CASCADE,
        related_name="contract_review",
    )
    document = models.ForeignKey(
        "documents.Document", on_delete=models.CASCADE, related_name="contract_reviews"
    )
    extraction_run = models.OneToOneField(
        "workflows.ExtractionRun",
        null=True,
        on_delete=models.CASCADE,
        related_name="contract_review",
    )
    revision = models.PositiveIntegerField(default=1)
    normalized = models.JSONField(default=dict)
    classification = models.JSONField(default=dict)
    validation = models.JSONField(default=list)
    coverage = models.JSONField(default=dict)
    effective_confidence = models.DecimalField(
        max_digits=5, decimal_places=4, null=True
    )
    finalized_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=["company", "document"])]


class ContractReviewOutput(ImmutableVersionPart):
    source_pins = models.JSONField(default=list)
    contract_review = models.ForeignKey(
        ContractReview, on_delete=models.CASCADE, related_name="outputs"
    )
    revision = models.PositiveIntegerField()
    schema_version = models.PositiveIntegerField(default=1)
    data = models.JSONField()
    digest = models.CharField(max_length=64)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["contract_review", "revision"], name="contract_output_revision"
            )
        ]
