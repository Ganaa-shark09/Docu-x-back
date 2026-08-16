from django.test import SimpleTestCase

from apps.ai_engine.services.external_document_readiness_service import (
    ExternalDocumentReadinessService,
)


class ExternalDocumentReadinessServiceTests(SimpleTestCase):
    def test_normalize_uuids_deduplicates_and_accepts_csv(self):
        self.assertEqual(
            ExternalDocumentReadinessService.normalize_uuids("a,b,a,,c"),
            ["a", "b", "c"],
        )

        self.assertEqual(
            ExternalDocumentReadinessService.normalize_uuids(["a", " b ", "a"]),
            ["a", "b"],
        )

    def test_serialize_status_requires_ready_status_and_ai_ready(self):
        from types import SimpleNamespace

        ready = SimpleNamespace(
            uuid="ready-uuid",
            title="Ready document",
            original_filename="ready.txt",
            status="ready",
            is_ai_ready=True,
            is_ocr_completed=True,
            is_indexed=True,
            processing_error="",
            updated_at=None,
        )
        processing = SimpleNamespace(
            uuid="processing-uuid",
            title="Processing document",
            original_filename="processing.txt",
            status="processing",
            is_ai_ready=False,
            is_ocr_completed=False,
            is_indexed=False,
            processing_error="",
            updated_at=None,
        )
        failed = SimpleNamespace(
            uuid="failed-uuid",
            title="Failed document",
            original_filename="failed.txt",
            status="failed",
            is_ai_ready=False,
            is_ocr_completed=False,
            is_indexed=False,
            processing_error="Extraction failed",
            updated_at=None,
        )

        self.assertEqual(
            ExternalDocumentReadinessService.serialize_status(ready)["state"],
            "ready",
        )
        self.assertEqual(
            ExternalDocumentReadinessService.serialize_status(processing)["state"],
            "processing",
        )
        self.assertEqual(
            ExternalDocumentReadinessService.serialize_status(failed)["state"],
            "failed",
        )
