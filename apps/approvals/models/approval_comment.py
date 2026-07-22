from django.conf import settings
from django.db import models

from apps.approvals.models.approval_request import ApprovalRequest
from apps.shared.models import BaseModel
from apps.tenants.models import Company


class ApprovalComment(BaseModel):
    class CommentType(models.TextChoices):
        GENERAL = "general", "General"
        CORRECTION = "correction", "Correction"
        SYSTEM = "system", "System"
        DECISION = "decision", "Decision"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="approval_comments",
    )

    approval_request = models.ForeignKey(
        ApprovalRequest,
        on_delete=models.CASCADE,
        related_name="comments",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="approval_comments",
        null=True,
        blank=True,
    )

    comment_type = models.CharField(
        max_length=50,
        choices=CommentType.choices,
        default=CommentType.GENERAL,
        db_index=True,
    )

    comment = models.TextField()
    field_path = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional JSON field path for correction comments, e.g. invoice.total_amount.",
    )
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if self.approval_request_id and not self.company_id:
            self.company = self.approval_request.company

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.approval_request.title} - {self.comment_type}"

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["company", "comment_type"]),
            models.Index(fields=["approval_request", "created_at"]),
        ]
