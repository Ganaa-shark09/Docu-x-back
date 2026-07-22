from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from apps.tenants.models import Company


class AuditLog(BaseModel):
    class EventType(models.TextChoices):
        AI_CHAT_REQUEST = "ai_chat_request", "AI Chat Request"
        AI_CONTEXT_USED = "ai_context_used", "AI Context Used"
        AI_CONTEXT_REDACTED = "ai_context_redacted", "AI Context Redacted"
        AI_CONTEXT_BLOCKED = "ai_context_blocked", "AI Context Blocked"
        AI_PROVIDER_ROUTED = "ai_provider_routed", "AI Provider Routed"
        DOCUMENT_ACCESS = "document_access", "Document Access"
        SECURITY_EVENT = "security_event", "Security Event"

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="audit_logs",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        null=True,
        blank=True,
    )

    event_type = models.CharField(
        max_length=80,
        choices=EventType.choices,
        db_index=True,
    )

    severity = models.CharField(
        max_length=30,
        choices=Severity.choices,
        default=Severity.INFO,
        db_index=True,
    )

    action = models.CharField(max_length=255)
    resource_type = models.CharField(max_length=100, blank=True)
    resource_uuid = models.UUIDField(null=True, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.company.name} - {self.event_type} - {self.created_at}"

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "event_type"]),
            models.Index(fields=["company", "severity"]),
            models.Index(fields=["company", "created_at"]),
            models.Index(fields=["resource_type", "resource_uuid"]),
        ]
