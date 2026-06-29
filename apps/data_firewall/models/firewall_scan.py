from django.db import models

from apps.documents.models import Document
from apps.shared.models import BaseModel


class FirewallScan(BaseModel):
    class RiskLevel(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    class Decision(models.TextChoices):
        ALLOW = "allow", "Allow"
        REDACT_AND_ALLOW = "redact_and_allow", "Redact and Allow"
        REQUIRE_APPROVAL = "require_approval", "Require Approval"
        BLOCK_CLOUD_LLM = "block_cloud_llm", "Block Cloud LLM"

    document = models.OneToOneField(
        Document,
        on_delete=models.CASCADE,
        related_name="firewall_scan",
    )

    risk_level = models.CharField(
        max_length=50,
        choices=RiskLevel.choices,
        default=RiskLevel.LOW,
        db_index=True,
    )

    decision = models.CharField(
        max_length=50,
        choices=Decision.choices,
        default=Decision.ALLOW,
        db_index=True,
    )

    detected_patterns = models.JSONField(default=list, blank=True)
    redacted_preview = models.TextField(blank=True)
    scan_summary = models.TextField(blank=True)

    can_use_cloud_llm = models.BooleanField(default=True)
    requires_human_approval = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.document.title} - {self.risk_level}"

    class Meta:
        ordering = ["-created_at"]
