import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TransactionTestCase
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.document_processing.models import DocumentTextExtraction
from apps.documents.models import Document
from apps.tenants.models import Company
from apps.vector_search.services.vector_indexing_service import VectorIndexingService
from apps.workflows.models import (
    HRPolicy,
    HRPolicyCategory,
    HRPolicyVersion,
    HRPolicyAnswer,
)
from apps.workflows.services.hr_policy_service import HRPolicyService
from apps.workflows.services.hr_policy_workflow_service import HRPolicyWorkflowService
from apps.workflows.services.hr_policy_qa_service import HRPolicyQAService
from apps.workflows.services.workflow_engine_service import (
    WorkflowEngineService,
    WorkflowConflict,
)
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService
from apps.workflows.tests.test_hr_policy_workflow import TEXT


class HRPolicyConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Concurrent HR", slug="concurrent-hr"
        )
        self.admin = User.objects.create_user(
            username="concurrent-hr", company=self.company, role="company_admin"
        )
        self.category = HRPolicyCategory.objects.create(
            company=self.company, kind="policy", code="leave", name="Leave"
        )
        self.doc = Document.objects.create(
            company=self.company,
            uploaded_by=self.admin,
            title="Policy",
            original_filename="policy.txt",
            status="ready",
            is_ai_ready=True,
        )
        DocumentTextExtraction.objects.create(
            document=self.doc,
            extracted_text=TEXT,
            extraction_method="plain_text",
            page_count=1,
            confidence_score=100,
        )
        VectorIndexingService.index_document(self.doc)
        self.policy = HRPolicyService.create(
            self.admin,
            title="Leave",
            code="leave",
            category_uuid=self.category.uuid,
            draft={"document_uuid": str(self.doc.uuid), "effective_from": "2026-01-01"},
        )
        self.enqueue = patch.object(WorkflowEngineService, "enqueue")
        self.enqueue.start()
        self.addCleanup(self.enqueue.stop)

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

    def submit(self, key="start"):
        return HRPolicyWorkflowService.submit(
            User.objects.get(pk=self.admin.pk),
            HRPolicy.objects.get(pk=self.policy.pk),
            revision=1,
            reviewer={"user": self.admin.pk},
            approver={"user": self.admin.pk},
            idempotency_key=key,
        )

    def advance(self, run, until=None):
        for _ in range(12):
            run.refresh_from_db()
            if run.status != "queued" or run.current_step.key == until:
                return
            WorkflowExecutorService.execute(
                self.company.pk, str(run.uuid), run.revision
            )
        self.fail("Policy did not reach a workflow boundary")

    def test_duplicate_start_is_one_review_run(self):
        results = self.race(lambda _: str(self.submit().uuid))
        # A contender that read the pre-start snapshot retries after a revision conflict.
        if "denied" in results:
            self.assertEqual(
                str(self.submit().uuid), next(r for r in results if r != "denied")
            )
        else:
            self.assertEqual(results[0], results[1])
        self.assertEqual(self.company.workflowrun_set.count(), 1)

    def test_competing_draft_changes_are_revision_safe(self):
        def edit(index):
            HRPolicyService.edit(
                User.objects.get(pk=self.admin.pk),
                HRPolicy.objects.get(pk=self.policy.pk),
                1,
                {
                    "document_uuid": str(self.doc.uuid),
                    "effective_from": f"2026-0{index + 2}-01",
                },
            )
            return "edited"

        self.assertCountEqual(self.race(edit), ["edited", "denied"])

    def test_duplicate_publication_workers_create_one_version_and_sections(self):
        run = self.submit()
        self.advance(run)
        WorkflowEngineService.command(
            self.admin,
            run,
            action="accept",
            expected_revision=run.revision,
            idempotency_key="review",
        )
        self.advance(run)
        WorkflowEngineService.command(
            self.admin,
            run,
            action="approve",
            expected_revision=run.revision,
            idempotency_key="approval",
        )
        self.advance(run, "publish_policy")
        revision = run.revision
        self.assertCountEqual(
            self.race(
                lambda _: WorkflowExecutorService.execute(
                    self.company.pk, str(run.uuid), revision
                )
            ),
            [True, False],
        )
        self.assertEqual(HRPolicyVersion.objects.filter(policy=self.policy).count(), 1)
        self.assertEqual(
            HRPolicyVersion.objects.get(policy=self.policy).sections.count(), 2
        )

    def test_duplicate_question_persistence_is_idempotent(self):
        results = self.race(
            lambda _: str(
                HRPolicyQAService.chat(
                    User.objects.get(pk=self.admin.pk),
                    "What is the leave policy?",
                    idempotency_key="same-question",
                )["assistant_message"].uuid
            )
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(HRPolicyAnswer.objects.count(), 1)
