from django.utils import timezone

from config.celery import app
from apps.data_firewall.services import FirewallScanService
from apps.document_processing.services import (
    DocumentClassificationService,
    TextExtractionService,
)
from apps.documents.models import Document, DocumentProcessingJob


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
    job.error_message = ""
    job.progress_percent = 10
    job.save(
        update_fields=[
            "status",
            "started_at",
            "error_message",
            "progress_percent",
            "updated_at",
        ]
    )

    return job


def _mark_job_success(job, output_payload=None):
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

    document.status = Document.Status.PROCESSING
    document.processing_error = ""
    document.save(update_fields=["status", "processing_error", "updated_at"])

    try:
        extraction_result = run_text_extraction(document.uuid)
        classification_result = run_document_classification(document.uuid)
        firewall_result = run_firewall_scan(document.uuid)

        document.refresh_from_db()

        document.is_ai_ready = bool(document.is_ocr_completed)
        document.is_indexed = False
        document.status = (
            Document.Status.READY if document.is_ai_ready else Document.Status.FAILED
        )

        if not document.is_ai_ready:
            document.processing_error = "No extractable text found in document."

        document.save(
            update_fields=[
                "is_ai_ready",
                "is_indexed",
                "status",
                "processing_error",
                "updated_at",
            ]
        )

        return {
            "document_uuid": str(document.uuid),
            "text_extraction": extraction_result,
            "classification": classification_result,
            "firewall": firewall_result,
            "status": document.status,
        }

    except Exception as exc:
        document.status = Document.Status.FAILED
        document.processing_error = str(exc)
        document.save(update_fields=["status", "processing_error", "updated_at"])
        raise self.retry(exc=exc, countdown=10)


def run_text_extraction(document_uuid):
    document = Document.objects.get(uuid=document_uuid)

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
