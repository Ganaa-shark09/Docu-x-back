from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.shared.models import BaseModel
from apps.tenants.models import Company


class Notification(BaseModel):
    class NotificationType(models.TextChoices):
        APPROVAL_ASSIGNED = "approval_assigned", "Approval Assigned"
        APPROVAL_APPROVED = "approval_approved", "Approval Approved"
        APPROVAL_REJECTED = "approval_rejected", "Approval Rejected"
        APPROVAL_CHANGES_REQUESTED = (
            "approval_changes_requested",
            "Approval Changes Requested",
        )
        APPROVAL_COMMENT_ADDED = "approval_comment_added", "Approval Comment Added"
        DOCUMENT_PROCESSING_COMPLETED = (
            "document_processing_completed",
            "Document Processing Completed",
        )
        DOCUMENT_PROCESSING_FAILED = (
            "document_processing_failed",
            "Document Processing Failed",
        )
        EXTRACTION_COMPLETED = "extraction_completed", "Extraction Completed"
        EXTRACTION_FAILED = "extraction_failed", "Extraction Failed"
        SYSTEM = "system", "System"

    class Status(models.TextChoices):
        UNREAD = "unread", "Unread"
        READ = "read", "Read"
        ARCHIVED = "archived", "Archived"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="notifications",
    )

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="triggered_notifications",
        null=True,
        blank=True,
    )

    notification_type = models.CharField(
        max_length=80,
        choices=NotificationType.choices,
        db_index=True,
    )

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.UNREAD,
        db_index=True,
    )

    priority = models.CharField(
        max_length=30,
        choices=Priority.choices,
        default=Priority.NORMAL,
        db_index=True,
    )

    title = models.CharField(max_length=255)
    message = models.TextField(blank=True)

    resource_type = models.CharField(max_length=100, blank=True)
    resource_uuid = models.UUIDField(null=True, blank=True)

    action_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="Frontend route or API URL related to this notification.",
    )

    metadata = models.JSONField(default=dict, blank=True)

    read_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    def mark_read(self):
        self.status = self.Status.READ
        self.read_at = timezone.now()
        self.save(update_fields=["status", "read_at", "updated_at"])

    def archive(self):
        self.status = self.Status.ARCHIVED
        self.archived_at = timezone.now()
        self.save(update_fields=["status", "archived_at", "updated_at"])

    def __str__(self):
        return f"{self.recipient} - {self.title}"

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "recipient"]),
            models.Index(fields=["company", "recipient", "status"]),
            models.Index(fields=["company", "notification_type"]),
            models.Index(fields=["resource_type", "resource_uuid"]),
        ]
