from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from datetime import timedelta

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

    class ProcessingStage(models.TextChoices):
        QUEUED = "queued", "Queued"
        EXTRACTING = "extracting", "Extracting"
        CLASSIFYING = "classifying", "Classifying"
        FIREWALL_SCAN = "firewall_scan", "Firewall Scan"
        CHUNKING = "chunking", "Chunking"
        EMBEDDING = "embedding", "Embedding"
        INDEXING = "indexing", "Indexing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    class Scope(models.TextChoices):
        INTERNAL = "internal", "Internal"
        EXTERNAL = "external", "External"

    class Sensitivity(models.TextChoices):
        PUBLIC = "public", "Public"
        INTERNAL = "internal", "Internal"
        CONFIDENTIAL = "confidential", "Confidential"
        HIGHLY_CONFIDENTIAL = "highly_confidential", "Highly Confidential"
        RESTRICTED = "restricted", "Restricted"

    scope = models.CharField(
        max_length=30,
        choices=Scope.choices,
        default=Scope.INTERNAL,
        db_index=True,
        help_text="Internal documents are company knowledge. External documents are user/chat uploads.",
    )

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

    owner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_documents",
    )
    custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="custodian_documents",
    )
    retention_policy = models.ForeignKey(
        "documents.RetentionPolicy", on_delete=models.PROTECT, null=True, blank=True
    )
    retention_duration_days = models.PositiveIntegerField(null=True, blank=True)
    retention_start = models.DateTimeField(null=True, blank=True)
    retention_expires_at = models.DateTimeField(null=True, blank=True)
    legal_hold = models.BooleanField(default=False)
    governance_revision = models.PositiveIntegerField(default=1)

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

    processing_stage = models.CharField(
        max_length=40,
        choices=ProcessingStage.choices,
        default=ProcessingStage.QUEUED,
        db_index=True,
    )
    processing_progress = models.PositiveSmallIntegerField(default=0)
    processing_error = models.TextField(blank=True)

    def clean(self):
        for name in (
            "department",
            "owner_user",
            "custodian",
            "retention_policy",
            "uploaded_by",
        ):
            if (
                getattr(self, name + "_id")
                and getattr(self, name).company_id != self.company_id
            ):
                raise ValidationError(
                    {name: "Reference must belong to the owning company."}
                )

    def save(self, *args, **kwargs):
        self.clean()
        if self._state.adding and self.retention_start is None:
            self.retention_start = self.created_at or timezone.now()
            self.retention_duration_days = (
                self.retention_policy.duration_days
                if self.retention_policy_id
                else max(1, self.company.data_retention_days)
            )
            self.retention_expires_at = self.retention_start + timedelta(
                days=self.retention_duration_days
            )
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "retention_expires_at"]),
            models.Index(fields=["company", "scope"]),
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "document_type"]),
            models.Index(fields=["company", "sensitivity_label"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(retention_duration_days__isnull=True)
                | models.Q(retention_duration_days__gte=1),
                name="document_retention_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(retention_expires_at__isnull=True)
                | models.Q(retention_start__isnull=True)
                | models.Q(retention_expires_at__gte=models.F("retention_start")),
                name="document_retention_order",
            ),
        ]
