"""Fail-closed regression tests for accounting handoff provider responses.

No ERP is called: a deterministic fake connector models malformed receipts and
reconciliation responses. Original invoice contents must never be leaked.
"""

import uuid
from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.documents.models import Document
from apps.integrations.services.invoice_accounting import InvoiceAccountingService
from apps.tenants.models import Company
from apps.workflows.models import (
    InvoiceAccountingHandoff,
    InvoiceOutputVersion,
    InvoiceRecord,
)
from apps.workflows.services.invoice_definition_service import InvoiceDefinitionService
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
)


class InvoiceReceiptConnector:
    allowed_sensitivities = ("internal",)
    post_reference = "ERP-POST-001"
    lookup_reference = "ERP-LOOKUP-001"
    post_calls = 0
    lookup_calls = 0

    def post(self, *, company_id, payload, idempotency_key):
        type(self).post_calls += 1
        return {"status": "posted", "external_reference": type(self).post_reference}

    def lookup(self, *, company_id, idempotency_key):
        type(self).lookup_calls += 1
        return {"status": "posted", "external_reference": type(self).lookup_reference}


@override_settings(
    DOCUX_INVOICE_ACCOUNTING_CONNECTOR=(
        "apps.workflows.tests.test_invoice_accounting_references.InvoiceReceiptConnector"
    ),
    CELERY_TASK_ALWAYS_EAGER=False,
)
class InvoiceAccountingReferenceTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="ERP Receipts", slug="erp-receipts")
        cls.admin = User.objects.create_user(
            username="erp-receipts-admin", company=cls.company, role="company_admin"
        )
        _, cls.version = InvoiceDefinitionService.create(
            cls.admin,
            name="ERP Receipts",
            code="erp-receipts",
            reviewer={"user": cls.admin.pk},
            policy={"require_approval": False},
        )

    def setUp(self):
        InvoiceReceiptConnector.post_reference = "ERP-POST-001"
        InvoiceReceiptConnector.lookup_reference = "ERP-LOOKUP-001"
        InvoiceReceiptConnector.post_calls = 0
        InvoiceReceiptConnector.lookup_calls = 0
        self.enqueue_workflow = patch.object(WorkflowEngineService, "enqueue")
        self.enqueue_workflow.start()
        self.addCleanup(self.enqueue_workflow.stop)
        post_patcher = patch.object(InvoiceAccountingService, "enqueue")
        self.enqueue_post = post_patcher.start()
        self.addCleanup(post_patcher.stop)

    def final_invoice(self):
        document = Document.objects.create(
            document_type="invoice",
            company=self.company,
            uploaded_by=self.admin,
            title="Invoice",
            original_filename="invoice.txt",
            status="ready",
            is_ai_ready=True,
        )
        run, _ = WorkflowEngineService.start(
            self.admin,
            version_uuid=self.version.uuid,
            document_uuids=[str(document.uuid)],
            idempotency_key=uuid.uuid4().hex,
        )
        type(run).objects.filter(pk=run.pk).update(status="completed")
        run.status = "completed"
        invoice = InvoiceRecord.objects.create(
            company=self.company, document=document, workflow_run=run
        )
        InvoiceOutputVersion.objects.create(
            invoice=invoice,
            revision=1,
            data={"invoice": {"invoice_number": "INV"}},
            digest="test-digest",
        )
        return invoice

    def test_posted_requires_visible_nonblank_bounded_reference(self):
        invalid_references = (
            "",
            "  ",
            " ERP-1",
            "ERP-1 ",
            "ERP\n1",
            "ERP\x00",
            "X" * 256,
            42,
        )
        for index, reference in enumerate(invalid_references):
            with self.subTest(reference=repr(reference)[:32]):
                invoice = self.final_invoice()
                handoff, _ = InvoiceAccountingService.request_post(
                    self.admin, invoice, f"malformed-{index}"
                )
                InvoiceReceiptConnector.post_reference = reference
                self.assertFalse(
                    InvoiceAccountingService.process(self.company.pk, str(handoff.uuid))
                )
                handoff.refresh_from_db()
                self.assertEqual(handoff.status, "uncertain")
                self.assertEqual(handoff.failure_code, "confirmation_required")
                self.assertEqual(handoff.external_reference, "")
        self.assertEqual(InvoiceReceiptConnector.post_calls, len(invalid_references))

    def test_valid_posted_reference_remains_idempotent(self):
        invoice = self.final_invoice()
        handoff, _ = InvoiceAccountingService.request_post(self.admin, invoice, "valid")
        self.assertTrue(
            InvoiceAccountingService.process(self.company.pk, str(handoff.uuid))
        )
        self.assertFalse(
            InvoiceAccountingService.process(self.company.pk, str(handoff.uuid))
        )
        handoff.refresh_from_db()
        self.assertEqual(handoff.status, "posted")
        self.assertEqual(handoff.external_reference, "ERP-POST-001")
        self.assertEqual(InvoiceReceiptConnector.post_calls, 1)

    def test_reconcile_does_not_confirm_invalid_provider_reference(self):
        invoice = self.final_invoice()
        handoff, _ = InvoiceAccountingService.request_post(
            self.admin, invoice, "reconcile"
        )
        type(handoff).objects.filter(pk=handoff.pk).update(status="uncertain")
        self.enqueue_post.reset_mock()
        InvoiceReceiptConnector.lookup_reference = "\nERP"
        returned = InvoiceAccountingService.reconcile(self.admin, invoice)
        self.assertEqual(returned.status, "uncertain")
        self.assertEqual(returned.external_reference, "")
        self.enqueue_post.assert_not_called()
        InvoiceReceiptConnector.lookup_reference = "ERP-LOOKUP-001"
        returned = InvoiceAccountingService.reconcile(self.admin, invoice)
        self.assertEqual(returned.status, "posted")
        self.assertEqual(returned.external_reference, "ERP-LOOKUP-001")
        self.assertEqual(InvoiceReceiptConnector.lookup_calls, 2)
        self.enqueue_post.assert_not_called()

    def test_soft_deleted_handoff_cannot_be_replayed(self):
        invoice = self.final_invoice()
        handoff, _ = InvoiceAccountingService.request_post(
            self.admin, invoice, "replay-key"
        )
        InvoiceAccountingHandoff.objects.filter(pk=handoff.pk).update(is_deleted=True)
        with self.assertRaises(WorkflowConflict):
            InvoiceAccountingService.request_post(self.admin, invoice, "replay-key")
        self.assertEqual(
            InvoiceAccountingHandoff.objects.filter(invoice=invoice).count(), 1
        )
        self.assertEqual(InvoiceReceiptConnector.post_calls, 0)
