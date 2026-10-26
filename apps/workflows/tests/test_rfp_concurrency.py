import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections
from django.test import TransactionTestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.workflows.models import RFPResponseVersion, WorkflowRun
from apps.workflows.services.rfp_service import RFPService
from apps.workflows.services.workflow_engine_service import (
    WorkflowEngineService,
    WorkflowConflict,
)
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService
from apps.workflows.tests import test_rfp_workflow as fixtures


class RFPConcurrencyTests(TransactionTestCase):
    document = fixtures.RFPWorkflowTests.document
    submit = fixtures.RFPWorkflowTests.submit
    advance = fixtures.RFPWorkflowTests.advance
    review = fixtures.RFPWorkflowTests.review
    proof = fixtures.RFPWorkflowTests.proof
    ready_proposal = fixtures.RFPWorkflowTests.ready_proposal

    def setUp(self):
        fixtures.RFPWorkflowTests.setUpTestData.__func__(type(self))
        self.client = APIClient()
        fixtures.RFPWorkflowTests.setUp(self)

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

    def test_concurrent_same_key_starts_return_one_run(self):
        result = self.race(lambda _: self.submit().pk)
        self.assertEqual(result[0], result[1])
        self.assertEqual(
            WorkflowRun.objects.filter(
                metadata__rfp_project_uuid=str(self.project.uuid)
            ).count(),
            1,
        )

    def test_concurrent_distinct_starts_do_not_duplicate(self):
        result = self.race(lambda i: self.submit(key="start-" + str(i)).pk)
        self.assertEqual(result.count("denied"), 1)

    def test_concurrent_response_edits_do_not_overwrite(self):
        self.review()
        item = self.project.items.first()

        def edit(index):
            return RFPService.edit(
                self.admin,
                self.project,
                item,
                revision=item.revision,
                text=fixtures.KNOWLEDGE,
                evidence=[self.proof()],
                compliance="compliant",
                reason="Concurrent review " + str(index),
                externally_shareable=True,
            ).pk

        result = self.race(edit)
        self.assertEqual(result.count("denied"), 1)
        self.assertEqual(RFPResponseVersion.objects.filter(item=item).count(), 2)

    def test_duplicate_workers_consume_one_revision(self):
        run = self.submit()
        result = self.race(
            lambda _: WorkflowExecutorService.execute(
                run.company_id, str(run.uuid), run.revision
            )
        )
        self.assertEqual(result.count(True), 1)
        self.assertEqual(self.project.items.count(), 4)

    def test_concurrent_assembly_creates_one_output(self):
        run = self.ready_proposal()
        WorkflowEngineService.command(
            self.admin,
            run,
            action="approve",
            expected_revision=run.revision,
            idempotency_key="concurrent-final-approval",
        )
        self.project.refresh_from_db()
        result = self.race(lambda _: RFPService.assemble(self.project).pk)
        self.assertEqual(result[0], result[1])
        self.assertEqual(self.project.outputs.count(), 1)
