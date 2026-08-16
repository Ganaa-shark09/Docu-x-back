from django.utils import timezone

from config.celery import app
from apps.data_firewall.services import FirewallScanService
from apps.document_processing.services import (
    DocumentClassificationService,
    TextExtractionService,
)
from apps.documents.models import Document, DocumentProcessingJob
from apps.documents.services import DocumentProcessingEventService
from apps.vector_search.services import VectorIndexingService


def _mark_job_running(document, job_type):
    job = (
        DocumentProcessingJob.objects.filter(
            document=document,
            job_type=job_type,
        )
        .order_by("-created_at")
        .first()
    )

    if not job:
        job = DocumentProcessingJob.objects.create(
            document=document,
            job_type=job_type,
        )

    job.status = DocumentProcessingJob.Status.RUNNING
    job.started_at = timezone.now()
    job.completed_at = None
    job.error_message = ""
    job.progress_percent = 10
    job.save(
        update_fields=[
            "status",
            "started_at",
            "completed_at",
            "error_message",
            "progress_percent",
            "updated_at",
        ]
    )

    return job


def _mark_job_success(job, output_payload=None):
    if not job or job.status == DocumentProcessingJob.Status.SUCCESS:
        return

    job.status = DocumentProcessingJob.Status.SUCCESS
    job.completed_at = timezone.now()
    job.progress_percent = 100
    job.output_payload = output_payload or {}
    job.save(
        update_fields=[
            "status",
            "completed_at",
            "progress_percent",
            "output_payload",
            "updated_at",
        ]
    )


def _mark_job_failed(job, error):
    if not job:
        return

    job.status = DocumentProcessingJob.Status.FAILED
    job.completed_at = timezone.now()
    job.progress_percent = 100
    job.error_message = str(error)
    job.save(
        update_fields=[
            "status",
            "completed_at",
            "progress_percent",
            "error_message",
            "updated_at",
        ]
    )


@app.task(bind=True, max_retries=2)
def process_document_pipeline(self, document_uuid):
    document = Document.objects.get(uuid=document_uuid)

    document.is_ai_ready = False
    document.processing_error = ""
    document.save(
        update_fields=[
            "is_ai_ready",
            "processing_error",
            "updated_at",
        ]
    )

    try:
        extraction_result = run_text_extraction(document.uuid)
        classification_result = run_document_classification(document.uuid)
        firewall_result = run_firewall_scan(document.uuid)
        indexing_result = run_vector_indexing(document.uuid)

        document.refresh_from_db()

        document.is_ai_ready = bool(document.is_ocr_completed and document.is_indexed)

        if not document.is_ai_ready:
            document.processing_error = (
                "Document processing completed, but document is not AI ready."
            )
            document.save(
                update_fields=[
                    "is_ai_ready",
                    "processing_error",
                    "updated_at",
                ]
            )
            DocumentProcessingEventService.transition(
                document,
                stage=Document.ProcessingStage.FAILED,
                error=document.processing_error,
                message="Document processing completed but the document is not AI ready",
            )
        else:
            document.processing_error = ""
            document.save(
                update_fields=[
                    "is_ai_ready",
                    "processing_error",
                    "updated_at",
                ]
            )
            DocumentProcessingEventService.transition(
                document,
                stage=Document.ProcessingStage.READY,
                progress=100,
                message="Document is ready for AI",
            )

        return {
            "document_uuid": str(document.uuid),
            "text_extraction": extraction_result,
            "classification": classification_result,
            "firewall": firewall_result,
            "indexing": indexing_result,
            "status": document.status,
            "processing_stage": document.processing_stage,
        }

    except Exception as exc:
        document.refresh_from_db()

        if self.request.retries < self.max_retries:
            retry_number = self.request.retries + 1
            DocumentProcessingEventService.transition(
                document,
                stage=Document.ProcessingStage.QUEUED,
                progress=0,
                status=Document.Status.PROCESSING,
                message=f"Retrying document processing ({retry_number}/{self.max_retries})",
                error=None,
                extra={
                    "retry_number": retry_number,
                    "max_retries": self.max_retries,
                    "last_error": str(exc),
                },
            )
            raise self.retry(exc=exc, countdown=10)

        DocumentProcessingEventService.transition(
            document,
            stage=Document.ProcessingStage.FAILED,
            error=exc,
            message="Document processing failed",
        )
        raise


def run_text_extraction(document_uuid):
    document = Document.objects.get(uuid=document_uuid)
    DocumentProcessingEventService.transition(
        document,
        stage=Document.ProcessingStage.EXTRACTING,
        progress=10,
        message="Extracting document text",
    )

    job = _mark_job_running(document, DocumentProcessingJob.JobType.OCR)

    try:
        extraction = TextExtractionService.extract_for_document(document)

        output = {
            "method": extraction.extraction_method,
            "page_count": extraction.page_count,
            "character_count": extraction.character_count,
            "word_count": extraction.word_count,
            "confidence_score": str(extraction.confidence_score),
        }

        _mark_job_success(job, output)

        return output

    except Exception as exc:
        _mark_job_failed(job, exc)
        raise


def run_document_classification(document_uuid):
    document = Document.objects.get(uuid=document_uuid)
    DocumentProcessingEventService.transition(
        document,
        stage=Document.ProcessingStage.CLASSIFYING,
        progress=35,
        message="Classifying document",
    )

    job = _mark_job_running(document, DocumentProcessingJob.JobType.CLASSIFICATION)

    try:
        result = DocumentClassificationService.classify(document)
        _mark_job_success(job, result)
        return result

    except Exception as exc:
        _mark_job_failed(job, exc)
        raise


def run_firewall_scan(document_uuid):
    document = Document.objects.get(uuid=document_uuid)
    DocumentProcessingEventService.transition(
        document,
        stage=Document.ProcessingStage.FIREWALL_SCAN,
        progress=45,
        message="Running data firewall checks",
    )

    job = _mark_job_running(document, DocumentProcessingJob.JobType.FIREWALL_SCAN)

    try:
        scan = FirewallScanService.scan_document(document)

        result = {
            "risk_level": scan.risk_level,
            "decision": scan.decision,
            "detected_patterns": scan.detected_patterns,
            "can_use_cloud_llm": scan.can_use_cloud_llm,
            "requires_human_approval": scan.requires_human_approval,
        }

        _mark_job_success(job, result)

        return result

    except Exception as exc:
        _mark_job_failed(job, exc)
        raise


def run_vector_indexing(document_uuid):
    document = Document.objects.get(uuid=document_uuid)

    jobs = {
        "chunking": _mark_job_running(
            document,
            DocumentProcessingJob.JobType.CHUNKING,
        ),
        "embedding": None,
        "indexing": None,
    }
    stage_metadata = {}

    DocumentProcessingEventService.transition(
        document,
        stage=Document.ProcessingStage.CHUNKING,
        progress=55,
        message="Creating document chunks",
    )

    def progress_callback(stage, metadata):
        nonlocal jobs, stage_metadata
        metadata = metadata or {}
        stage_metadata[stage] = metadata

        if stage == "chunking":
            # The CHUNKING stage was emitted immediately before indexing work
            # started. Keep the computed chunk count for job output without
            # sending a duplicate stage event.
            return

        if stage == "embedding":
            _mark_job_success(
                jobs["chunking"],
                stage_metadata.get("chunking", {}),
            )
            jobs["embedding"] = jobs["embedding"] or _mark_job_running(
                document,
                DocumentProcessingJob.JobType.EMBEDDING,
            )
            DocumentProcessingEventService.transition(
                document,
                stage=Document.ProcessingStage.EMBEDDING,
                progress=70,
                message="Creating embeddings",
                extra=metadata,
            )
            return

        if stage == "indexing":
            _mark_job_success(
                jobs["embedding"],
                stage_metadata.get("embedding", {}),
            )
            jobs["indexing"] = jobs["indexing"] or _mark_job_running(
                document,
                DocumentProcessingJob.JobType.INDEXING,
            )
            DocumentProcessingEventService.transition(
                document,
                stage=Document.ProcessingStage.INDEXING,
                progress=90,
                message="Indexing document",
                extra=metadata,
            )

    try:
        result = VectorIndexingService.index_document(
            document,
            progress_callback=progress_callback,
        )

        # Handle providers/edge cases that return before one of the normal
        # callbacks so processing-job state still reaches a terminal state.
        _mark_job_success(
            jobs["chunking"],
            stage_metadata.get(
                "chunking", {"chunk_count": result.get("chunk_count", 0)}
            ),
        )

        if jobs["embedding"] is None:
            jobs["embedding"] = _mark_job_running(
                document,
                DocumentProcessingJob.JobType.EMBEDDING,
            )
        _mark_job_success(
            jobs["embedding"],
            {
                "embedding_provider": result.get("embedding_provider"),
                "embedding_dimensions": result.get("embedding_dimensions"),
            },
        )

        if jobs["indexing"] is None:
            jobs["indexing"] = _mark_job_running(
                document,
                DocumentProcessingJob.JobType.INDEXING,
            )
        _mark_job_success(jobs["indexing"], result)

        return result

    except Exception as exc:
        _mark_job_failed(jobs.get("chunking"), exc)
        _mark_job_failed(jobs.get("embedding"), exc)
        _mark_job_failed(jobs.get("indexing"), exc)
        raise
