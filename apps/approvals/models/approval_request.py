from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.documents.models import Document
from apps.shared.models import BaseModel
from apps.tenants.models import Company


class ApprovalRequest(BaseModel):
    class ReviewType(models.TextChoices):
        DOCUMENT_REVIEW = "document_review", "Document Review"
        EXTRACTION_REVIEW = "extraction_review", "Extraction Review"
        FIREWALL_REVIEW = "firewall_review", "Firewall Review"
        EXPORT_APPROVAL = "export_approval", "Export Approval"
        AI_OUTPUT_REVIEW = "ai_output_review", "AI Output Review"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending"
        IN_REVIEW = "in_review", "In Review"
        CHANGES_REQUESTED = "changes_requested", "Changes Requested"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="approval_requests",
    )

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="approval_requests",
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="created_approval_requests",
        null=True,
        blank=True,
    )

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="assigned_approval_requests",
        null=True,
        blank=True,
    )

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_approval_requests",
        null=True,
        blank=True,
    )

    review_type = models.CharField(
        max_length=50,
        choices=ReviewType.choices,
        default=ReviewType.DOCUMENT_REVIEW,
        db_index=True,
    )

    status = models.CharField(
        max_length=50,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    priority = models.CharField(
        max_length=30,
        choices=Priority.choices,
        default=Priority.MEDIUM,
        db_index=True,
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    submitted_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="AI-generated extraction/report/output submitted for human review.",
    )

    reviewed_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Human-corrected final payload after review.",
    )

    reviewer_notes = models.TextField(blank=True)
    due_at = models.DateTimeField(null=True, blank=True)

    submitted_at = models.DateTimeField(default=timezone.now)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        if self.document and self.company_id != self.document.company_id:
            raise ValidationError("Document must belong to the same company.")

        if self.requested_by and self.requested_by.company_id != self.company_id:
            raise ValidationError("Requester must belong to the same company.")

        if self.assigned_to and self.assigned_to.company_id != self.company_id:
            raise ValidationError("Reviewer must belong to the same company.")

    def save(self, *args, **kwargs):
        if self.document_id and not self.company_id:
            self.company = self.document.company

        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def is_closed(self):
        return self.status in [
            self.Status.APPROVED,
            self.Status.REJECTED,
            self.Status.CANCELLED,
        ]

    def __str__(self):
        return f"{self.title} - {self.status}"

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "review_type"]),
            models.Index(fields=["company", "assigned_to"]),
            models.Index(fields=["company", "requested_by"]),
            models.Index(fields=["document", "status"]),
        ]
