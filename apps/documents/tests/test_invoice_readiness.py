from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.document_processing.models import DocumentTextExtraction
from apps.documents.models import Document
from apps.tenants.models import Company
from apps.documents.services.invoice_readiness_service import InvoiceReadinessService


class InvoiceReadinessTests(SimpleTestCase):
    @staticmethod
    def document(
        *,
        document_type="invoice",
        text="Invoice Number: 147\nInvoice Date: 2026-10-26\nVendor: Demo\nCurrency: INR\nSubtotal: 100\nGrand Total: 118",
        status="ready",
        scope="internal",
    ):
        return SimpleNamespace(
            scope=scope,
            is_deleted=False,
            document_type=document_type,
            status=status,
            is_ai_ready=True,
            is_indexed=True,
            text_extraction=SimpleNamespace(extracted_text=text),
        )

    def test_complete_source_has_readiness_without_exposing_content(self):
        readiness = InvoiceReadinessService.assess(self.document())
        self.assertEqual(readiness["status"], "ready")
        self.assertTrue(readiness["can_start_invoice_workflow"])
        self.assertEqual(readiness["missing_signals"], [])
        self.assertNotIn("Demo", str(readiness))
        self.assertNotIn("147", str(readiness))

    def test_imperfect_invoice_is_not_rejected_for_missing_fields(self):
        readiness = InvoiceReadinessService.assess(
            self.document(text="Invoice Number: 147")
        )
        self.assertEqual(readiness["status"], "needs_review")
        self.assertTrue(readiness["can_start_invoice_workflow"])
        self.assertIn("grand_total", readiness["missing_signals"])

    def test_expired_retention_fails_closed(self):
        from datetime import timedelta
        from django.utils import timezone

        doc = self.document()
        doc.retention_expires_at = timezone.now() - timedelta(days=1)
        self.assertEqual(InvoiceReadinessService.assess(doc)["status"], "blocked")

    def test_non_invoice_metadata_fails_closed(self):
        readiness = InvoiceReadinessService.assess(
            self.document(document_type="contract")
        )
        self.assertEqual(readiness["status"], "blocked")
        self.assertFalse(readiness["can_start_invoice_workflow"])

    def test_obvious_contract_content_blocks_invoice_submission(self):
        readiness = InvoiceReadinessService.assess(
            self.document(
                text="This agreement is binding. Governing law applies. Termination clause."
            )
        )
        self.assertEqual(readiness["status"], "blocked")

    def test_no_text_requires_review_but_no_external_llm(self):
        readiness = InvoiceReadinessService.assess(self.document(text=""))
        self.assertEqual(readiness["status"], "needs_review")
        self.assertTrue(readiness["can_start_invoice_workflow"])

    def test_queued_invoice_can_be_processed_after_ingestion(self):
        readiness = InvoiceReadinessService.assess(self.document(status="processing"))
        self.assertEqual(readiness["status"], "processing")

    def test_field_restrictions_withhold_source_hints(self):
        with patch(
            "apps.documents.services.invoice_readiness_service.FieldPermissionService.has_restrictions",
            return_value=True,
        ):
            readiness = InvoiceReadinessService.assess(self.document(), user=object())
        self.assertEqual(readiness["status"], "restricted")
        self.assertEqual(readiness["checks"], [])
        self.assertFalse(readiness["can_start_invoice_workflow"])

    @patch(
        "apps.documents.services.invoice_readiness_service.FieldPermissionService.has_restrictions",
        return_value=False,
    )
    @patch(
        "apps.documents.services.invoice_readiness_service.DocumentPermissionService.can_action",
        return_value=True,
    )
    def test_batch_gate_rejects_misclassified_documents(self, *_):
        with self.assertRaises(ValidationError):
            InvoiceReadinessService.require_batch_source(
                object(), self.document(document_type="other")
            )

    @patch(
        "apps.documents.services.invoice_readiness_service.FieldPermissionService.has_restrictions",
        return_value=False,
    )
    @patch(
        "apps.documents.services.invoice_readiness_service.DocumentPermissionService.can_action",
        return_value=False,
    )
    def test_batch_gate_respects_edit_permission(self, *_):
        with self.assertRaises(PermissionDenied):
            InvoiceReadinessService.require_batch_source(object(), self.document())


class InvoiceReadinessApiTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Preflight", slug="preflight")
        self.other_company = Company.objects.create(
            name="Foreign preflight", slug="foreign-preflight"
        )
        self.admin = User.objects.create_user(
            username="preflight-admin", company=self.company, role="company_admin"
        )
        self.client.force_authenticate(self.admin)
        self.document = Document.objects.create(
            company=self.company,
            uploaded_by=self.admin,
            document_type="invoice",
            title="Invoice",
            original_filename="invoice.txt",
            status="ready",
            is_ai_ready=True,
            is_indexed=True,
        )
        DocumentTextExtraction.objects.create(
            document=self.document,
            extracted_text="Invoice Number: 1\nVendor: Test\nGrand Total: 50",
        )

    def test_get_readiness_uses_document_authorization(self):
        response = self.client.get(
            f"/api/documents/{self.document.uuid}/invoice-readiness/"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "needs_review")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertNotIn("Test", str(response.data))

    def test_foreign_company_document_cannot_be_probed(self):
        foreign = Document.objects.create(
            company=self.other_company,
            document_type="invoice",
            title="Foreign",
            original_filename="foreign.txt",
            status="ready",
        )
        response = self.client.get(f"/api/documents/{foreign.uuid}/invoice-readiness/")
        self.assertEqual(response.status_code, 404)
