from unittest.mock import patch

from django.test import TestCase

from apps.documents.models import Document
from apps.documents.services import (
    DocumentProcessingJobService,
    DocumentProcessingQueueService,
)
from apps.tenants.models import Company


class DocumentProcessingQueueServiceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Queue Co", slug="queue-co")
        self.document = Document.objects.create(
            company=self.company,
            title="queued.pdf",
            original_filename="queued.pdf",
            scope=Document.Scope.INTERNAL,
        )
        DocumentProcessingJobService.create_default_jobs_for_document(self.document)

    @patch("celery.canvas.Signature.apply_async")
    def test_enqueue_dispatches_only_after_commit_and_persists_task_id(
        self, apply_async
    ):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            task_id = DocumentProcessingQueueService.enqueue(self.document)
            self.assertFalse(apply_async.called)

        self.assertTrue(task_id)
        self.assertGreaterEqual(
            len(callbacks), 2
        )  # websocket publish + Celery dispatch

        task_ids = set(
            self.document.processing_jobs.values_list("celery_task_id", flat=True)
        )
        self.assertEqual(task_ids, {task_id})

        for callback in callbacks:
            callback()

        apply_async.assert_called_once_with(
            task_id=task_id, queue="document_processing"
        )

        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_stage,
            Document.ProcessingStage.QUEUED,
        )
        self.assertEqual(self.document.processing_progress, 0)

    @patch("celery.canvas.Signature.apply_async")
    def test_enqueue_reset_state_clears_terminal_processing_state(self, apply_async):
        self.document.status = Document.Status.FAILED
        self.document.processing_stage = Document.ProcessingStage.FAILED
        self.document.processing_progress = 100
        self.document.processing_error = "old failure"
        self.document.is_ai_ready = True
        self.document.is_indexed = True
        self.document.is_ocr_completed = True
        self.document.save()

        with self.captureOnCommitCallbacks(execute=True):
            DocumentProcessingQueueService.enqueue(self.document, reset_state=True)

        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.Status.UPLOADED)
        self.assertEqual(
            self.document.processing_stage, Document.ProcessingStage.QUEUED
        )
        self.assertEqual(self.document.processing_progress, 0)
        self.assertEqual(self.document.processing_error, "")
        self.assertFalse(self.document.is_ai_ready)
        self.assertFalse(self.document.is_indexed)
        self.assertFalse(self.document.is_ocr_completed)
        apply_async.assert_called_once()
