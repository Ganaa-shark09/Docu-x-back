from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from apps.tenants.models import Company
from apps.workspaces.models import Department


class AIConversation(BaseModel):
    class Scope(models.TextChoices):
        INTERNAL = "internal", "Internal"
        EXTERNAL = "external", "External"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    scope = models.CharField(
        max_length=30,
        choices=Scope.choices,
        default=Scope.INTERNAL,
        db_index=True,
    )

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="ai_conversations",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_conversations",
    )

    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        related_name="ai_conversations",
        null=True,
        blank=True,
    )

    title = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=50,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )

    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.title or f"Conversation {self.uuid}"

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["company", "scope"]),
            models.Index(fields=["company", "user"]),
            models.Index(fields=["company", "department"]),
            models.Index(fields=["company", "status"]),
        ]
