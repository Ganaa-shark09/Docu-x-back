import uuid
import logging

from django.conf import settings
from django.db import transaction

from apps.documents.models import Document, DocumentProcessingJob
from apps.documents.services.document_processing_event_service import (
    DocumentProcessingEventService,
)

logger = logging.getLogger(__name__)


class DocumentProcessingQueueService:
    """Enqueue the document pipeline only after the surrounding DB commit.

    This service is the single entry point for upload/retry queueing. It keeps
    database state, Celery task identity, and realtime queued-state publication
    consistent and avoids workers racing an uncommitted Document row.
    """

    @classmethod
    def enqueue(cls, document, *, reset_state=False):
        if reset_state:
            document.status = Document.Status.UPLOADED
            document.processing_stage = Document.ProcessingStage.QUEUED
            document.processing_progress = 0
            document.processing_error = ""
            document.is_ocr_completed = False
            document.is_ai_ready = False
            document.is_indexed = False
            document.save(
                update_fields=[
                    "status",
                    "processing_stage",
                    "processing_progress",
                    "processing_error",
                    "is_ocr_completed",
                    "is_ai_ready",
                    "is_indexed",
                    "updated_at",
                ]
            )

        from apps.documents.tasks import process_document_pipeline

        signature = process_document_pipeline.s(str(document.uuid))
        task_id = str(uuid.uuid4())
        queue_name = getattr(
            settings, "DOCUMENT_PROCESSING_QUEUE", "document_processing"
        )

        document.processing_jobs.filter(
            status=DocumentProcessingJob.Status.PENDING,
            is_deleted=False,
        ).update(celery_task_id=task_id)

        DocumentProcessingEventService.transition(
            document,
            stage=Document.ProcessingStage.QUEUED,
            progress=0,
            status=Document.Status.UPLOADED,
            message="Queued for processing",
            extra={"celery_task_id": task_id, "queue": queue_name},
        )

        def _dispatch():
            try:
                signature.apply_async(task_id=task_id, queue=queue_name)
                logger.info(
                    "document_processing_enqueued document=%s task_id=%s queue=%s",
                    document.uuid,
                    task_id,
                    queue_name,
                )
            except Exception:
                logger.exception(
                    "document_processing_enqueue_failed document=%s task_id=%s",
                    document.uuid,
                    task_id,
                )
                # Queue publication failure should be reflected in the durable
                # document state so reconnecting clients do not remain at queued.
                fresh_document = Document.objects.filter(uuid=document.uuid).first()
                if fresh_document:
                    DocumentProcessingEventService.transition(
                        fresh_document,
                        stage=Document.ProcessingStage.FAILED,
                        error="Document processing could not be queued.",
                        message="Document processing queue failed",
                    )

        transaction.on_commit(_dispatch)
        return task_id
