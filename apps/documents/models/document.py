from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from apps.tenants.models import Company
from apps.workspaces.models import Department


class Document(BaseModel):
    class DocumentType(models.TextChoices):
        INVOICE = "invoice", "Invoice"
        CONTRACT = "contract", "Contract"
        POLICY = "policy", "Policy"
        RESUME = "resume", "Resume"
        PURCHASE_ORDER = "purchase_order", "Purchase Order"
        BANK_STATEMENT = "bank_statement", "Bank Statement"
        REPORT = "report", "Report"
        LEGAL = "legal", "Legal"
        HR = "hr", "HR"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        ARCHIVED = "archived", "Archived"

    class Sensitivity(models.TextChoices):
        PUBLIC = "public", "Public"
        INTERNAL = "internal", "Internal"
        CONFIDENTIAL = "confidential", "Confidential"
        HIGHLY_CONFIDENTIAL = "highly_confidential", "Highly Confidential"
        RESTRICTED = "restricted", "Restricted"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="documents",
    )

    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        related_name="documents",
        null=True,
        blank=True,
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="uploaded_documents",
        null=True,
        blank=True,
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    document_type = models.CharField(
        max_length=50,
        choices=DocumentType.choices,
        default=DocumentType.OTHER,
        db_index=True,
    )

    status = models.CharField(
        max_length=50,
        choices=Status.choices,
        default=Status.UPLOADED,
        db_index=True,
    )

    sensitivity_label = models.CharField(
        max_length=50,
        choices=Sensitivity.choices,
        default=Sensitivity.INTERNAL,
        db_index=True,
    )

    original_filename = models.CharField(max_length=255)
    file = models.FileField(upload_to="documents/%Y/%m/%d/")
    file_size = models.PositiveBigIntegerField(default=0)
    mime_type = models.CharField(max_length=150, blank=True)
    checksum = models.CharField(max_length=128, blank=True, db_index=True)

    page_count = models.PositiveIntegerField(default=0)
    language = models.CharField(max_length=50, blank=True)

    is_indexed = models.BooleanField(default=False)
    is_ocr_completed = models.BooleanField(default=False)
    is_ai_ready = models.BooleanField(default=False)

    processing_error = models.TextField(blank=True)

    def __str__(self):
        return self.title

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "document_type"]),
            models.Index(fields=["company", "sensitivity_label"]),
        ]
