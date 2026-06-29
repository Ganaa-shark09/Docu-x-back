from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from apps.documents.models.document import Document


class DocumentVersion(BaseModel):
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="versions",
    )

    version_number = models.PositiveIntegerField(default=1)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="document_versions",
        null=True,
        blank=True,
    )

    file = models.FileField(upload_to="document_versions/%Y/%m/%d/")
    original_filename = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField(default=0)
    mime_type = models.CharField(max_length=150, blank=True)
    checksum = models.CharField(max_length=128, blank=True, db_index=True)

    notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.document.title} - v{self.version_number}"

    class Meta:
        unique_together = ("document", "version_number")
        ordering = ["document", "-version_number"]
