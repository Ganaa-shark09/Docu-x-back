from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from apps.workflows.models.engine import ImmutableVersionPart


class InvoiceVendor(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    identifier = models.CharField(max_length=150, blank=True)
    tax_id = models.CharField(max_length=150, blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["company", "identifier"]),
            models.Index(fields=["company", "tax_id"]),
            models.Index(fields=["company", "name"]),
        ]


class InvoiceBatch(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    version = models.ForeignKey("workflows.WorkflowVersion", on_delete=models.PROTECT)
    name = models.CharField(max_length=150)
    idempotency_key = models.CharField(max_length=128)
    request_fingerprint = models.CharField(max_length=64)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "idempotency_key"], name="invoice_batch_idempotency"
            ),
        ]


class InvoiceBatchItem(BaseModel):
    batch = models.ForeignKey(
        InvoiceBatch, on_delete=models.CASCADE, related_name="items"
    )
    document = models.ForeignKey("documents.Document", on_delete=models.PROTECT)
    run = models.OneToOneField(
        "workflows.WorkflowRun", null=True, on_delete=models.PROTECT
    )
    position = models.PositiveIntegerField()
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "document"], name="invoice_batch_document"
            ),
            models.UniqueConstraint(
                fields=["batch", "position"], name="invoice_batch_position"
            ),
        ]


class InvoiceRecord(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.CASCADE)
    workflow_run = models.OneToOneField(
        "workflows.WorkflowRun", on_delete=models.CASCADE, related_name="invoice"
    )
    document = models.ForeignKey(
        "documents.Document", on_delete=models.CASCADE, related_name="invoices"
    )
    extraction_run = models.OneToOneField(
        "workflows.ExtractionRun",
        null=True,
        on_delete=models.CASCADE,
        related_name="invoice",
    )
    revision = models.PositiveIntegerField(default=1)
    normalized = models.JSONField(default=dict)
    classification = models.JSONField(default=dict)
    validation = models.JSONField(default=list)
    matches = models.JSONField(default=dict)
    duplicates = models.JSONField(default=list)
    invoice_number = models.CharField(max_length=150, blank=True)
    vendor_key = models.CharField(max_length=255, blank=True)
    invoice_date = models.DateField(null=True)
    fingerprint = models.CharField(max_length=64, blank=True)
    effective_confidence = models.DecimalField(
        max_digits=5, decimal_places=4, null=True
    )

    class Meta:
        indexes = [
            models.Index(fields=["company", "invoice_number", "vendor_key"]),
            models.Index(fields=["company", "fingerprint"]),
            models.Index(fields=["company", "invoice_date"]),
        ]


class InvoiceOutputVersion(ImmutableVersionPart):
    source_pins = models.JSONField(default=list)
    invoice = models.ForeignKey(
        InvoiceRecord, on_delete=models.CASCADE, related_name="outputs"
    )
    revision = models.PositiveIntegerField()
    schema_version = models.PositiveIntegerField(default=1)
    data = models.JSONField()
    digest = models.CharField(max_length=64)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "revision"], name="invoice_output_revision"
            )
        ]


class InvoiceAccountingHandoff(BaseModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        POSTED = "posted", "Posted"
        UNCERTAIN = "uncertain", "Confirmation required"
        REJECTED = "rejected", "Rejected"

    invoice = models.ForeignKey(
        InvoiceRecord, on_delete=models.PROTECT, related_name="accounting_handoffs"
    )
    output = models.ForeignKey(InvoiceOutputVersion, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    idempotency_key = models.CharField(max_length=128)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    external_reference = models.CharField(max_length=255, blank=True)
    failure_code = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "idempotency_key"],
                name="invoice_handoff_idempotency",
            ),
        ]


class ExtractionCorrection(ImmutableVersionPart):
    source_metadata = models.JSONField(default=dict, blank=True)
    field = models.ForeignKey(
        "workflows.ExtractionResultField",
        on_delete=models.CASCADE,
        related_name="corrections",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    revision = models.PositiveIntegerField()
    previous_value = models.TextField(blank=True)
    corrected_value = models.TextField(blank=True)
    reason = models.CharField(max_length=4000)

    class Meta:
        ordering = ["revision", "created_at"]
