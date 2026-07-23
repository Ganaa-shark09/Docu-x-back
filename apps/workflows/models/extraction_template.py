from django.db import models

from apps.shared.models import BaseModel
from apps.tenants.models import Company


class ExtractionTemplate(BaseModel):
    class TemplateType(models.TextChoices):
        INVOICE = "invoice", "Invoice"
        CONTRACT = "contract", "Contract"
        POLICY = "policy", "Policy"
        RESUME = "resume", "Resume"
        PURCHASE_ORDER = "purchase_order", "Purchase Order"
        CUSTOM = "custom", "Custom"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="extraction_templates",
        null=True,
        blank=True,
        help_text="Null means system/global template.",
    )

    name = models.CharField(max_length=255)
    template_type = models.CharField(
        max_length=50,
        choices=TemplateType.choices,
        default=TemplateType.CUSTOM,
        db_index=True,
    )

    description = models.TextField(blank=True)
    is_system = models.BooleanField(default=False, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)

    output_schema = models.JSONField(default=dict, blank=True)
    instructions = models.TextField(blank=True)

    def __str__(self):
        scope = (
            "System"
            if self.is_system
            else self.company.name
            if self.company
            else "Global"
        )
        return f"{scope} - {self.name}"

    class Meta:
        ordering = ["-is_system", "name"]
        indexes = [
            models.Index(fields=["company", "template_type"]),
            models.Index(fields=["company", "is_active"]),
            models.Index(fields=["is_system", "is_active"]),
        ]
