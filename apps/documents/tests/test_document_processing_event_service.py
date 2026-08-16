from unittest.mock import patch

from django.test import TestCase

from apps.documents.models import Document
from apps.documents.services.document_processing_event_service import (
    DocumentProcessingEventService,
)
from apps.tenants.models import Company


class DocumentProcessingEventServiceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Realtime Co", slug="realtime-co")
        self.document = Document.objects.create(
            company=self.company,
            title="policy.pdf",
            original_filename="policy.pdf",
            scope=Document.Scope.INTERNAL,
        )

    @patch.object(DocumentProcessingEventService, "publish_payload")
    def test_transition_persists_state_before_publishing(self, publish_payload):
        with self.captureOnCommitCallbacks(execute=True):
            payload = DocumentProcessingEventService.transition(
                self.document,
                stage=Document.ProcessingStage.EMBEDDING,
                progress=70,
                message="Creating embeddings",
            )

        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.Status.PROCESSING)
        self.assertEqual(
            self.document.processing_stage,
            Document.ProcessingStage.EMBEDDING,
        )
        self.assertEqual(self.document.processing_progress, 70)
        self.assertEqual(payload["event"], "document.processing.updated")
        self.assertEqual(payload["document_uuid"], str(self.document.uuid))
        publish_payload.assert_called_once()

    @patch.object(DocumentProcessingEventService, "publish_payload")
    def test_ready_transition_sets_terminal_state(self, publish_payload):
        self.document.is_ai_ready = True
        self.document.is_indexed = True
        self.document.is_ocr_completed = True
        self.document.save()

        with self.captureOnCommitCallbacks(execute=True):
            payload = DocumentProcessingEventService.transition(
                self.document,
                stage=Document.ProcessingStage.READY,
                progress=100,
            )

        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.Status.READY)
        self.assertEqual(self.document.processing_stage, Document.ProcessingStage.READY)
        self.assertEqual(self.document.processing_progress, 100)
        self.assertTrue(payload["is_ai_ready"])
        publish_payload.assert_called_once()
