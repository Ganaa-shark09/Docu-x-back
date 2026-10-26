"""Invoice exception resolution must respect current, visible review state."""

from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.document_processing.models import DocumentTextExtraction
from apps.documents.models import Document
from apps.tenants.models import Company
from apps.workflows.models import WorkflowCommand
from apps.workflows.services.invoice_definition_service import InvoiceDefinitionService
from apps.workflows.services.workflow_engine_service import WorkflowEngineService
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
class InvoiceExceptionRecoveryTests(TestCase):
    TEXT = (
        "Tax invoice\nInvoice number: 000123\nInvoice date: 2026-10-07\n"
        "Due date: 2026-11-07\nVendor name: Acme\nVendor identifier: 0007\n"
        "Bill to name: Buyer\nCurrency: USD\nSubtotal: 100.00\n"
        "Tax total: 10.00\nGrand total: 110.00\nAmount due: 110.00\n"
    )

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Invoice exception regression", slug="invoice-exception-regression"
        )
        cls.admin = User.objects.create_user(
            username="invoice-exception-admin",
            company=cls.company,
            role="company_admin",
        )
        cls.document = Document.objects.create(
            document_type="invoice",
            company=cls.company,
            uploaded_by=cls.admin,
            title="Invoice source",
            original_filename="invoice.txt",
            status="ready",
            is_ai_ready=True,
            is_indexed=True,
        )
        DocumentTextExtraction.objects.create(
            document=cls.document,
            extraction_method="plain_text",
            extracted_text=cls.TEXT,
            page_count=1,
            confidence_score=100,
        )

    def setUp(self):
        enqueue_patcher = patch.object(WorkflowEngineService, "enqueue")
        enqueue_patcher.start()
        self.addCleanup(enqueue_patcher.stop)
        _, version = InvoiceDefinitionService.create(
            self.admin,
            name="Invoice exception test",
            code="invoice-exception-test",
            reviewer={"user": self.admin.pk},
            policy={"require_approval": False, "force_review": True},
        )
        self.run, created = WorkflowEngineService.start(
            self.admin,
            version_uuid=version.uuid,
            document_uuids=[str(self.document.uuid)],
            idempotency_key="invoice-exception-run",
        )
        self.assertTrue(created)
        for _ in range(30):
            self.run.refresh_from_db()
            if self.run.status != "queued":
                break
            WorkflowExecutorService.execute(
                self.company.pk, str(self.run.uuid), self.run.revision
            )
        self.assertEqual(self.run.status, "review_required")
        WorkflowEngineService.command(
            self.admin,
            self.run,
            action="request_changes",
            expected_revision=self.run.revision,
            idempotency_key="exception-request-changes",
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "exception")
        self.exception = self.run.exceptions.get(status="open", is_deleted=False)

    def resolve(self, key):
        return WorkflowEngineService.command(
            self.admin,
            self.run,
            action="resolve_exception",
            expected_revision=self.run.revision,
            idempotency_key=key,
            exception=str(self.exception.uuid),
            resolution="Reviewed source fields and corrected the exception.",
        )

    def test_live_exception_resolves_once_and_preserves_audit_state(self):
        revision = self.run.revision
        result = self.resolve("live-exception-resolution")
        self.assertEqual(result.status, "queued")
        self.exception.refresh_from_db()
        self.assertEqual(self.exception.status, "resolved")
        self.assertEqual(self.exception.resolved_by_id, self.admin.pk)
        self.assertTrue(self.exception.resolved_at)
        self.assertEqual(
            WorkflowCommand.objects.filter(
                run=self.run, key="live-exception-resolution"
            ).count(),
            1,
        )
        # A repeated client request returns the existing result, not a new transition.
        WorkflowEngineService.command(
            self.admin,
            self.run,
            action="resolve_exception",
            expected_revision=revision,
            idempotency_key="live-exception-resolution",
            exception=str(self.exception.uuid),
            resolution="Reviewed source fields and corrected the exception.",
        )
        self.assertEqual(
            WorkflowCommand.objects.filter(
                run=self.run, key="live-exception-resolution"
            ).count(),
            1,
        )

    def test_soft_deleted_exception_cannot_transition_invoice(self):
        self.exception.is_deleted = True
        self.exception.save(update_fields=["is_deleted", "updated_at"])
        with self.assertRaises(ValidationError):
            self.resolve("deleted-exception-replay")
        self.run.refresh_from_db()
        self.exception.refresh_from_db()
        self.assertEqual(self.run.status, "exception")
        self.assertEqual(self.exception.status, "open")
        self.assertFalse(
            WorkflowCommand.objects.filter(
                run=self.run, key="deleted-exception-replay"
            ).exists()
        )

    def test_exception_recovery_requires_waiting_step(self):
        step_run = self.run.step_runs.get(step=self.run.current_step)
        step_run.status = "completed"
        step_run.save(update_fields=["status", "updated_at"])
        with self.assertRaisesMessage(
            ValidationError,
            "Invoice exception recovery requires the active waiting step.",
        ):
            self.resolve("inactive-step-resolution")
        self.run.refresh_from_db()
        self.exception.refresh_from_db()
        self.assertEqual(self.run.status, "exception")
        self.assertEqual(self.exception.status, "open")
