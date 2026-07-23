from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from apps.workflows.models.extraction_run import ExtractionRun


class ExtractionResultField(BaseModel):
    extraction_run = models.ForeignKey(
        ExtractionRun,
        on_delete=models.CASCADE,
        related_name="result_fields",
    )

    field_key = models.SlugField(max_length=150)
    label = models.CharField(max_length=255)

    extracted_value = models.TextField(blank=True)
    normalized_value = models.JSONField(default=dict, blank=True)

    confidence_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
    )

    source_text = models.TextField(blank=True)
    source_metadata = models.JSONField(default=dict, blank=True)

    is_required = models.BooleanField(default=False)
    is_missing = models.BooleanField(default=False)
    is_corrected = models.BooleanField(default=False)

    corrected_value = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_extraction_fields",
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    def final_value(self):
        return self.corrected_value if self.is_corrected else self.extracted_value

    def __str__(self):
        return f"{self.extraction_run.uuid} - {self.field_key}"

    class Meta:
        unique_together = ("extraction_run", "field_key")
        ordering = ["extraction_run", "field_key"]
        indexes = [
            models.Index(fields=["extraction_run", "field_key"]),
            models.Index(fields=["is_missing", "is_corrected"]),
        ]
