from django.db import models

from apps.shared.models import BaseModel
from apps.documents.models.document import Document


class DocumentProcessingJob(BaseModel):
    class JobType(models.TextChoices):
        OCR = "ocr", "OCR"
        CLASSIFICATION = "classification", "Classification"
        FIREWALL_SCAN = "firewall_scan", "Firewall Scan"
        CHUNKING = "chunking", "Chunking"
        EMBEDDING = "embedding", "Embedding"
        INDEXING = "indexing", "Indexing"
        EXTRACTION = "extraction", "Extraction"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="processing_jobs",
    )

    job_type = models.CharField(max_length=50, choices=JobType.choices, db_index=True)
    status = models.CharField(
        max_length=50,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    progress_percent = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)

    input_payload = models.JSONField(default=dict, blank=True)
    output_payload = models.JSONField(default=dict, blank=True)

    celery_task_id = models.CharField(max_length=255, blank=True, db_index=True)

    def __str__(self):
        return f"{self.document.title} - {self.job_type} - {self.status}"

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["document", "job_type"]),
            models.Index(fields=["status", "job_type"]),
        ]
