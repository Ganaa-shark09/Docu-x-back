from django.db import models
from pgvector.django import VectorField

from apps.documents.models import Document
from apps.shared.models import BaseModel
from apps.tenants.models import Company
from apps.workspaces.models import Department


class DocumentChunk(BaseModel):
    EMBEDDING_DIMENSIONS = 384

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="document_chunks",
    )

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="chunks",
    )

    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        related_name="document_chunks",
        null=True,
        blank=True,
    )

    chunk_index = models.PositiveIntegerField()
    content = models.TextField()

    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS)

    page_number = models.PositiveIntegerField(null=True, blank=True)
    character_count = models.PositiveIntegerField(default=0)
    token_count = models.PositiveIntegerField(default=0)

    document_type = models.CharField(max_length=50, db_index=True)
    sensitivity_label = models.CharField(max_length=50, db_index=True)

    embedding_provider = models.CharField(max_length=100, default="local_hashing")
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.document.title} - chunk {self.chunk_index}"

    class Meta:
        unique_together = ("document", "chunk_index")
        ordering = ["document", "chunk_index"]
        indexes = [
            models.Index(fields=["company", "document"]),
            models.Index(fields=["company", "department"]),
            models.Index(fields=["company", "document_type"]),
            models.Index(fields=["company", "sensitivity_label"]),
        ]
