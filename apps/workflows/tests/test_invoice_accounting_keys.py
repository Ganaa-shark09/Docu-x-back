"""Regression tests for company/invoice-scoped ERP posting keys.

Legacy uncertain handoffs must keep their original provider lookup key. The
key is opaque in the provider API; invoice field values never form part of it.
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
    WorkflowRun,
)
from apps.workflows.services.invoice_definition_service import InvoiceDefinitionService
from apps.workflows.services.workflow_engine_service import WorkflowEngineService


class InvoiceScopedConnector:
    allowed_sensitivities = ("internal",)
    posts = []
    lookups = []

    def post(self, *, company_id, payload, idempotency_key):
        type(self).posts.append((company_id, idempotency_key))
        return {"status": "posted", "external_reference": "ERP-TEST"}

    def lookup(self, *, company_id, idempotency_key):
        type(self).lookups.append((company_id, idempotency_key))
        return {"status": "posted", "external_reference": "ERP-LEGACY"}


@override_settings(
    DOCUX_INVOICE_ACCOUNTING_CONNECTOR=(
        "apps.workflows.tests.test_invoice_accounting_keys.InvoiceScopedConnector"
    ),
    CELERY_TASK_ALWAYS_EAGER=False,
)
class InvoiceAccountingKeyTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="ERP Keys", slug="erp-keys")
        cls.admin = User.objects.create_user(
            username="erp-keys-admin", company=cls.company, role="company_admin"
        )
        _, cls.version = InvoiceDefinitionService.create(
            cls.admin,
            name="ERP Keys",
            code="erp-keys",
            reviewer={"user": cls.admin.pk},
            policy={"require_approval": False},
        )

    def setUp(self):
        InvoiceScopedConnector.posts = []
        InvoiceScopedConnector.lookups = []
        self.start_patch = patch.object(WorkflowEngineService, "enqueue")
        self.start_patch.start()
        self.addCleanup(self.start_patch.stop)
        self.handoff_patch = patch.object(InvoiceAccountingService, "enqueue")
        self.handoff_patch.start()
        self.addCleanup(self.handoff_patch.stop)

    def make_final_invoice(self):
        doc = Document.objects.create(
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
            document_uuids=[str(doc.uuid)],
            idempotency_key=uuid.uuid4().hex,
        )
        WorkflowRun.objects.filter(pk=run.pk).update(status="completed")
        run.status = "completed"
        invoice = InvoiceRecord.objects.create(
            company=self.company, document=doc, workflow_run=run
        )
        output = InvoiceOutputVersion.objects.create(
            invoice=invoice,
            revision=1,
            data={"invoice": {"invoice_number": "INV"}},
            digest="test-digest",
        )
        return invoice, output

    def test_same_client_key_across_invoices_uses_distinct_provider_keys(self):
        first, _ = self.make_final_invoice()
        second, _ = self.make_final_invoice()
        first_handoff, created_first = InvoiceAccountingService.request_post(
            self.admin, first, "reused-from-browser"
        )
        second_handoff, created_second = InvoiceAccountingService.request_post(
            self.admin, second, "reused-from-browser"
        )
        self.assertTrue(created_first)
        self.assertTrue(created_second)
        self.assertNotEqual(
            first_handoff.idempotency_key, second_handoff.idempotency_key
        )
        self.assertTrue(first_handoff.idempotency_key.startswith("docux:v2:"))
        self.assertEqual(len(first_handoff.idempotency_key), 73)
        self.assertTrue(
            InvoiceAccountingService.process(self.company.pk, str(first_handoff.uuid))
        )
        self.assertTrue(
            InvoiceAccountingService.process(self.company.pk, str(second_handoff.uuid))
        )
        self.assertEqual(
            {entry[1] for entry in InvoiceScopedConnector.posts},
            {first_handoff.idempotency_key, second_handoff.idempotency_key},
        )

    def test_repeated_client_key_returns_same_handoff(self):
        invoice, _ = self.make_final_invoice()
        original, created = InvoiceAccountingService.request_post(
            self.admin, invoice, "key-1"
        )
        replay, created_again = InvoiceAccountingService.request_post(
            self.admin, invoice, "key-1"
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(original.pk, replay.pk)
        self.assertEqual(InvoiceAccountingHandoff.objects.count(), 1)

    def test_legacy_uncertain_handoff_reuses_original_lookup_key(self):
        invoice, output = self.make_final_invoice()
        legacy = InvoiceAccountingHandoff.objects.create(
            invoice=invoice,
            output=output,
            requested_by=self.admin,
            idempotency_key="legacy-erp-key",
            status="uncertain",
        )
        replay, created = InvoiceAccountingService.request_post(
            self.admin, invoice, "legacy-erp-key"
        )
        self.assertFalse(created)
        self.assertEqual(replay.pk, legacy.pk)
        resolved = InvoiceAccountingService.reconcile(self.admin, invoice)
        self.assertEqual(resolved.status, "posted")
        self.assertEqual(
            InvoiceScopedConnector.lookups, [(self.company.pk, "legacy-erp-key")]
        )
