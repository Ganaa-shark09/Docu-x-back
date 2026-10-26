"""Invoice finding decisions are immutable outside the active review step."""

from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.document_processing.models import DocumentTextExtraction
from apps.documents.models import Document
from apps.tenants.models import Company
from apps.workflows.models import WorkflowFinding
from apps.workflows.services.invoice_definition_service import InvoiceDefinitionService
from apps.workflows.services.workflow_engine_service import WorkflowEngineService
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
class InvoiceFindingLifecycleTests(TestCase):
    TEXT = (
        "Tax invoice\nInvoice number: 000123\nInvoice date: 2026-10-07\n"
        "Due date: 2026-11-07\nVendor name: Acme\nVendor identifier: 0007\n"
        "Bill to name: Buyer\nCurrency: USD\nSubtotal: 100.00\n"
        "Tax total: 10.00\nGrand total: 110.00\nAmount due: 110.00\n"
    )

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Invoice finding lifecycle", slug="invoice-finding-lifecycle"
        )
        cls.admin = User.objects.create_user(
            username="invoice-lifecycle-admin",
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
            name="Lifecycle invoice",
            code="lifecycle-invoice",
            reviewer={"user": self.admin.pk},
            policy={"require_approval": False, "force_review": True},
        )
        self.run, created = WorkflowEngineService.start(
            self.admin,
            version_uuid=version.uuid,
            document_uuids=[str(self.document.uuid)],
            idempotency_key="lifecycle-invoice-run",
        )
        self.assertTrue(created)

    def advance(self):
        for _ in range(30):
            self.run.refresh_from_db()
            if self.run.status != "queued":
                return
            WorkflowExecutorService.execute(
                self.company.pk, str(self.run.uuid), self.run.revision
            )
        self.fail("Invoice did not reach review or a terminal state")

    def finding(self, step_key="review_if_required"):
        return WorkflowFinding.objects.create(
            run=self.run,
            step_run=self.run.step_runs.get(step__key=step_key),
            category="manual_check",
            severity="low",
            title="Manual check",
            description="Lifecycle test finding",
        )

    def test_queued_invoice_rejects_premature_finding_decision(self):
        finding = self.finding()
        with self.assertRaises(ValidationError):
            WorkflowEngineService.review_finding(
                self.admin, self.run, finding.uuid, status="accepted"
            )
        finding.refresh_from_db()
        self.assertEqual(finding.status, "open")
        self.assertEqual(finding.review_state, "unreviewed")

    def test_review_decision_is_frozen_after_invoice_completion(self):
        self.advance()
        self.assertEqual(self.run.status, "review_required")
        finding = self.finding()
        WorkflowEngineService.review_finding(
            self.admin, self.run, finding.uuid, status="accepted"
        )
        finding.refresh_from_db()
        self.assertEqual(finding.review_state, "reviewed")
        self.assertEqual(finding.status, "accepted")
        WorkflowEngineService.command(
            self.admin,
            self.run,
            action="accept",
            expected_revision=self.run.revision,
            idempotency_key="lifecycle-accept",
        )
        self.advance()
        self.assertEqual(self.run.status, "completed")
        with self.assertRaises(ValidationError):
            WorkflowEngineService.review_finding(
                self.admin, self.run, finding.uuid, status="dismissed"
            )
        finding.refresh_from_db()
        self.assertEqual(finding.status, "accepted")

    def test_invoice_cannot_review_a_finding_from_another_step(self):
        self.advance()
        self.assertEqual(self.run.status, "review_required")
        finding = self.finding("validate_totals")
        with self.assertRaises(ValidationError):
            WorkflowEngineService.review_finding(
                self.admin, self.run, finding.uuid, status="accepted"
            )
        finding.refresh_from_db()
        self.assertEqual(finding.status, "open")
