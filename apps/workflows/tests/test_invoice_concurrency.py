import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.apps import apps
from django.db import close_old_connections
from django.test import TransactionTestCase
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.document_processing.models import DocumentTextExtraction
from apps.documents.models import Document
from apps.tenants.models import Company
from apps.workflows.models import InvoiceRecord, WorkflowRun
from apps.workflows.services.invoice_definition_service import InvoiceDefinitionService
from apps.workflows.services.invoice_workflow_service import InvoiceWorkflowService
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
)
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService


class InvoiceConcurrencyTests(TransactionTestCase):
    def setUp(self):
        # TransactionTestCase flushes migration seed data between test methods.
        importlib.import_module(
            "apps.workflows.migrations.0006_seed_invoice_schema"
        ).seed(apps, None)
        self.company = Company.objects.create(
            name="Concurrent invoice", slug="concurrent-invoice"
        )
        self.admin = User.objects.create_user(
            username="concurrent-invoice", company=self.company, role="company_admin"
        )
        self.document = Document.objects.create(
            document_type="invoice",
            company=self.company,
            uploaded_by=self.admin,
            title="Invoice",
            original_filename="invoice.txt",
            status="ready",
            is_ai_ready=True,
        )
        DocumentTextExtraction.objects.create(
            document=self.document,
            extracted_text="Tax invoice\nInvoice number: 0001\nInvoice date: 2026-10-07\nVendor name: Acme\nBill to name: Buyer\nCurrency: USD\nSubtotal: 100.00\nGrand total: 100.00\n",
            extraction_method="plain_text",
            page_count=1,
        )
        self.enqueue = patch.object(WorkflowEngineService, "enqueue")
        self.enqueue.start()
        self.addCleanup(self.enqueue.stop)
        _, self.version = InvoiceDefinitionService.create(
            self.admin,
            name="Concurrent",
            code="concurrent",
            reviewer={"user": self.admin.pk},
            policy={"require_approval": False, "force_review": True},
        )
        self.run, _ = WorkflowEngineService.start(
            self.admin,
            version_uuid=self.version.uuid,
            document_uuids=[str(self.document.uuid)],
            idempotency_key="invoice",
        )

    def race(self, operation):
        barrier = threading.Barrier(2)

        def worker(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation(index)
            except (WorkflowConflict, ValidationError):
                return "denied"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(worker, [0, 1]))

    def advance(self, until=None):
        for _ in range(30):
            self.run.refresh_from_db()
            if self.run.status != "queued" or self.run.current_step.key == until:
                return
            WorkflowExecutorService.execute(
                self.company.pk, str(self.run.uuid), self.run.revision
            )
        self.fail("Unbounded workflow processing")

    def test_duplicate_extraction_workers_commit_one_complete_field_set(self):
        self.advance("extract_invoice_fields")
        revision = self.run.revision
        result = self.race(
            lambda _: WorkflowExecutorService.execute(
                self.company.pk, str(self.run.uuid), revision
            )
        )
        self.assertCountEqual(result, [True, False])
        invoice = InvoiceRecord.objects.get(workflow_run=self.run)
        self.assertEqual(invoice.extraction_run.result_fields.count(), 23)
        self.assertEqual(self.run.step_runs.filter(extraction__isnull=False).count(), 1)

    def test_concurrent_corrections_preserve_one_revision_and_history(self):
        self.advance()
        revision = self.run.revision
        invoice = InvoiceRecord.objects.get(workflow_run=self.run)
        field_id = invoice.extraction_run.result_fields.get(field_key="vendor_name").pk

        def correct(index):
            from apps.workflows.models import ExtractionResultField

            InvoiceWorkflowService.correct(
                User.objects.get(pk=self.admin.pk),
                ExtractionResultField.objects.get(pk=field_id),
                f"Acme {index}",
                revision,
                "Reviewed",
            )
            return "corrected"

        self.assertCountEqual(self.race(correct), ["corrected", "denied"])
        field = invoice.extraction_run.result_fields.get(pk=field_id)
        invoice.refresh_from_db()
        self.assertEqual(invoice.revision, 2)
        self.assertEqual(field.extracted_value, "Acme")
        self.assertEqual(field.corrections.count(), 1)

    def test_concurrent_finalization_keeps_one_output(self):
        self.advance()
        WorkflowEngineService.command(
            self.admin,
            self.run,
            action="accept",
            expected_revision=self.run.revision,
            idempotency_key="review",
        )
        self.advance()
        self.assertEqual(self.run.status, "completed")
        invoice = InvoiceRecord.objects.get(workflow_run=self.run)

        def finalize(_):
            from apps.workflows.services.invoice_schema import InvoicePolicy

            InvoiceWorkflowService.finalize(
                InvoiceRecord.objects.get(pk=invoice.pk),
                InvoicePolicy.validate(
                    {"require_approval": False, "force_review": True}
                ),
            )
            return "finalized"

        self.assertEqual(self.race(finalize), ["finalized", "finalized"])
        self.assertEqual(invoice.outputs.count(), 1)
        self.assertEqual(WorkflowRun.objects.count(), 1)
