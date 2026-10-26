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
from apps.workflows.models import ContractReview, ExtractionResultField, WorkflowRun
from apps.workflows.services.contract_definition_service import (
    ContractDefinitionService,
)
from apps.workflows.services.contract_workflow_service import ContractWorkflowService
from apps.workflows.services.contract_schema import ContractPolicy, FIELDS
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
)
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService
from apps.workflows.tests.test_contract_workflow import TEXT


class ContractConcurrencyTests(TransactionTestCase):
    def setUp(self):
        importlib.import_module(
            "apps.workflows.migrations.0008_seed_contract_schema"
        ).seed(apps, None)
        self.company = Company.objects.create(
            name="Concurrent Contracts", slug="concurrent-contracts"
        )
        self.admin = User.objects.create_user(
            username="concurrent-contract", company=self.company, role="company_admin"
        )
        self.document = Document.objects.create(
            company=self.company,
            uploaded_by=self.admin,
            title="Contract",
            original_filename="contract.txt",
            status="ready",
            is_ai_ready=True,
        )
        DocumentTextExtraction.objects.create(
            document=self.document,
            extracted_text=TEXT,
            extraction_method="plain_text",
            page_count=1,
        )
        self.enqueue = patch.object(WorkflowEngineService, "enqueue")
        self.enqueue.start()
        self.addCleanup(self.enqueue.stop)
        _, self.version = ContractDefinitionService.create(
            self.admin,
            name="Concurrent",
            code="concurrent-contract",
            reviewer={"user": self.admin.pk},
            policy={"require_approval": False, "force_review": True},
        )
        self.run, _ = WorkflowEngineService.start(
            self.admin,
            version_uuid=self.version.uuid,
            document_uuids=[str(self.document.uuid)],
            idempotency_key="contract",
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
        for _ in range(35):
            self.run.refresh_from_db()
            if self.run.status != "queued" or self.run.current_step.key == until:
                return
            WorkflowExecutorService.execute(
                self.company.pk, str(self.run.uuid), self.run.revision
            )
        self.fail("Unbounded contract processing")

    def accept(self):
        WorkflowEngineService.command(
            self.admin,
            self.run,
            action="accept",
            expected_revision=self.run.revision,
            idempotency_key="accept",
        )

    def test_concurrent_start_returns_the_existing_idempotent_run(self):
        def start(_):
            run, created = WorkflowEngineService.start(
                User.objects.get(pk=self.admin.pk),
                version_uuid=self.version.uuid,
                document_uuids=[str(self.document.uuid)],
                idempotency_key="contract",
            )
            return str(run.uuid), created

        results = self.race(start)
        self.assertEqual(results, [(str(self.run.uuid), False)] * 2)
        self.assertEqual(WorkflowRun.objects.count(), 1)

    def test_duplicate_extraction_workers_persist_one_field_set(self):
        self.advance("extract_contract_metadata")
        revision = self.run.revision
        self.assertCountEqual(
            self.race(
                lambda _: WorkflowExecutorService.execute(
                    self.company.pk, str(self.run.uuid), revision
                )
            ),
            [True, False],
        )
        review = ContractReview.objects.get(workflow_run=self.run)
        self.assertEqual(review.extraction_run.result_fields.count(), len(FIELDS))

    def test_concurrent_corrections_preserve_one_revision(self):
        self.advance()
        review = ContractReview.objects.get(workflow_run=self.run)
        field = review.extraction_run.result_fields.get(field_key="effective_date")
        revision = self.run.revision

        def correct(index):
            ContractWorkflowService.correct(
                User.objects.get(pk=self.admin.pk),
                ExtractionResultField.objects.get(pk=field.pk),
                "2026-10-01",
                revision,
                f"Verified source {index}",
            )
            return "corrected"

        self.assertCountEqual(self.race(correct), ["corrected", "denied"])
        review.refresh_from_db()
        self.assertEqual(review.revision, 2)
        self.assertEqual(field.corrections.count(), 1)
        self.assertEqual(field.corrections.get().previous_value, "2026-10-01")

    def test_concurrent_review_submission_advances_once(self):
        self.advance()
        revision = self.run.revision

        def submit(index):
            WorkflowEngineService.command(
                User.objects.get(pk=self.admin.pk),
                WorkflowRun.objects.get(pk=self.run.pk),
                action="accept",
                expected_revision=revision,
                idempotency_key=f"review-{index}",
            )
            return "reviewed"

        self.assertCountEqual(self.race(submit), ["reviewed", "denied"])
        self.advance()
        self.assertEqual(self.run.status, "completed")
        self.assertEqual(self.run.contract_review.outputs.count(), 1)

    def test_concurrent_final_output_workers_create_one_output(self):
        self.advance()
        self.accept()
        self.advance("finalize_contract_review")
        revision = self.run.revision
        self.assertCountEqual(
            self.race(
                lambda _: WorkflowExecutorService.execute(
                    self.company.pk, str(self.run.uuid), revision
                )
            ),
            [True, False],
        )
        self.advance()
        review = ContractReview.objects.get(workflow_run=self.run)
        self.assertEqual(review.outputs.count(), 1)
        self.assertEqual(
            self.race(
                lambda _: ContractWorkflowService.finalize(
                    ContractReview.objects.get(pk=review.pk),
                    ContractPolicy.validate(
                        {"force_review": True, "require_approval": False}
                    ),
                )
            ),
            [review.outputs.get().data] * 2,
        )

    def test_concurrent_approvals_advance_once(self):
        _, version = ContractDefinitionService.create(
            self.admin,
            name="Approval",
            code="contract-approval",
            reviewer={"user": self.admin.pk},
            policy={"require_approval": True},
        )
        self.run, _ = WorkflowEngineService.start(
            self.admin,
            version_uuid=version.uuid,
            document_uuids=[str(self.document.uuid)],
            idempotency_key="approval-contract",
        )
        self.advance()
        self.assertEqual(self.run.status, "approval_required")
        revision = self.run.revision

        def approve(index):
            WorkflowEngineService.command(
                User.objects.get(pk=self.admin.pk),
                WorkflowRun.objects.get(pk=self.run.pk),
                action="approve",
                expected_revision=revision,
                idempotency_key=f"approval-{index}",
            )
            return "approved"

        self.assertCountEqual(self.race(approve), ["approved", "denied"])
        self.advance()
        self.assertEqual(self.run.status, "completed")
        self.assertEqual(self.run.contract_review.outputs.count(), 1)
