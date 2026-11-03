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


class ContractObligationAssignment(BaseModel):
    """Human-owned follow-up on a frozen, evidence-backed contract review row.

    Not an execution/signature record or an assertion that a legal duty was met.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed (attested)"

    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    review = models.ForeignKey(
        ContractReview, on_delete=models.PROTECT, related_name="obligation_assignments"
    )
    owner = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="contract_obligations"
    )
    created_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL,
        related_name="contract_obligations_created",
    )
    source_revision = models.PositiveIntegerField()
    source_index = models.PositiveIntegerField()
    source_digest = models.CharField(max_length=64)
    source_snapshot = models.JSONField(default=dict)
    governance_revision = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["review", "source_revision", "source_index"],
                name="contract_obligation_source_unique",
            )
        ]
        indexes = [models.Index(fields=["company", "status"], name="contract_ob_company_a35396_idx")]


class ContractObligationEvent(ImmutableVersionPart):
    """Append-only attestation/assignment history, with no confidential text."""

    obligation = models.ForeignKey(
        ContractObligationAssignment, on_delete=models.PROTECT, related_name="events"
    )
    actor = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL
    )
    sequence = models.PositiveIntegerField()
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    reason = models.CharField(max_length=500)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["obligation", "sequence"],
                name="contract_obligation_event_sequence",
            )
        ]
