from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.documents.models import Document
from apps.shared.models import BaseModel
from apps.tenants.models import Company
from apps.workflows.models.extraction_template import ExtractionTemplate


class ExtractionRun(BaseModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        REVIEW_REQUIRED = "review_required", "Review Required"
        SUBMITTED_FOR_APPROVAL = "submitted_for_approval", "Submitted for Approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="extraction_runs",
    )

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="extraction_runs",
    )

    template = models.ForeignKey(
        ExtractionTemplate,
        on_delete=models.PROTECT,
        related_name="extraction_runs",
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="extraction_runs",
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=50,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    overall_confidence = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
    )

    raw_output = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    approval_request_uuid = models.UUIDField(null=True, blank=True)

    def __str__(self):
        return f"{self.document.title} - {self.template.name} - {self.status}"

    def mark_running(self):
        self.status = self.Status.RUNNING
        self.started_at = timezone.now()
        self.error_message = ""
        self.save(update_fields=["status", "started_at", "error_message", "updated_at"])

    def mark_failed(self, error_message):
        self.status = self.Status.FAILED
        self.completed_at = timezone.now()
        self.error_message = error_message
        self.save(
            update_fields=["status", "completed_at", "error_message", "updated_at"]
        )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "document"]),
            models.Index(fields=["company", "template"]),
        ]
