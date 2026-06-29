from django.db import models

from apps.documents.models import Document
from apps.shared.models import BaseModel


class DocumentTextExtraction(BaseModel):
    class ExtractionMethod(models.TextChoices):
        PDF_TEXT = "pdf_text", "PDF Text"
        OCR = "ocr", "OCR"
        PLAIN_TEXT = "plain_text", "Plain Text"
        UNKNOWN = "unknown", "Unknown"

    document = models.OneToOneField(
        Document,
        on_delete=models.CASCADE,
        related_name="text_extraction",
    )

    extraction_method = models.CharField(
        max_length=50,
        choices=ExtractionMethod.choices,
        default=ExtractionMethod.UNKNOWN,
        db_index=True,
    )

    extracted_text = models.TextField(blank=True)
    page_count = models.PositiveIntegerField(default=0)
    character_count = models.PositiveIntegerField(default=0)
    word_count = models.PositiveIntegerField(default=0)

    confidence_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
    )

    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.document.title} - text extraction"

    class Meta:
        ordering = ["-created_at"]
