from django.db import models

from apps.shared.models import BaseModel
from apps.workflows.models.extraction_template import ExtractionTemplate


class ExtractionTemplateField(BaseModel):
    class FieldType(models.TextChoices):
        TEXT = "text", "Text"
        NUMBER = "number", "Number"
        DATE = "date", "Date"
        AMOUNT = "amount", "Amount"
        BOOLEAN = "boolean", "Boolean"
        LIST = "list", "List"
        JSON = "json", "JSON"

    template = models.ForeignKey(
        ExtractionTemplate,
        on_delete=models.CASCADE,
        related_name="fields",
    )

    field_key = models.SlugField(max_length=150)
    label = models.CharField(max_length=255)
    field_type = models.CharField(
        max_length=50,
        choices=FieldType.choices,
        default=FieldType.TEXT,
    )

    description = models.TextField(blank=True)
    extraction_hint = models.TextField(blank=True)
    regex_pattern = models.TextField(blank=True)

    is_required = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    default_value = models.TextField(blank=True)
    validation_rules = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.template.name} - {self.field_key}"

    class Meta:
        unique_together = ("template", "field_key")
        ordering = ["template", "order", "field_key"]
        indexes = [
            models.Index(fields=["template", "field_key"]),
        ]
