import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.workflows.services.finance_audit_service import FinanceAuditService as service
from apps.workflows.services.finance_workflow_service import FinanceWorkflowService
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
)
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService
from apps.workflows.tests import test_finance_audit as fixtures


@override_settings(MEDIA_ROOT="/tmp/docux-finance-tests")
class FinanceConcurrencyTests(TransactionTestCase):
    source = fixtures.FinanceAuditTests.source
    ingest = fixtures.FinanceAuditTests.ingest
    advance = fixtures.FinanceAuditTests.advance
    submit = fixtures.FinanceAuditTests.submit
    review = fixtures.FinanceAuditTests.review

    def setUp(self):
        fixtures.FinanceAuditTests.setUpTestData.__func__(type(self))
        self.client = APIClient()
        fixtures.FinanceAuditTests.setUp(self)

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

    def test_concurrent_import_same_key_returns_one_dataset(self):
        doc = self.source()

        def operation(_):
            return service.ingest(
                self.admin,
                self.audit,
                revision=1,
                document_uuid=doc.uuid,
                kind="json",
                mapping={},
                idempotency_key="same",
            ).pk

        result = self.race(operation)
        self.assertEqual(result[0], result[1])
        self.assertEqual(self.audit.datasets.count(), 1)

    def test_concurrent_import_distinct_keys_revision_safe(self):
        doc = self.source()
        result = self.race(
            lambda i: service.ingest(
                self.admin,
                self.audit,
                revision=1,
                document_uuid=doc.uuid,
                kind="json",
                mapping={},
                idempotency_key=str(i),
            ).pk
        )
        self.assertEqual(result.count("denied"), 1)
        self.assertEqual(self.audit.datasets.count(), 1)

    def test_concurrent_start_same_key_returns_one_run(self):
        self.ingest()
        result = self.race(
            lambda _: FinanceWorkflowService.submit(
                self.admin,
                self.audit,
                revision=2,
                reviewer={"user": self.admin.pk},
                approver={"user": self.admin.pk},
                idempotency_key="same",
            ).pk
        )
        self.assertEqual(result[0], result[1])

    def test_concurrent_corrections_do_not_overwrite(self):
        row = self.ingest().transactions.get()
        result = self.race(
            lambda i: service.correct(
                self.admin,
                self.audit,
                row.uuid,
                revision=2,
                values={**fixtures.RECORD, "gross_amount": str(i)},
                reason="Concurrent verified correction",
            ).pk
        )
        self.assertEqual(result.count("denied"), 1)
        self.assertEqual(row.corrections.count(), 1)

    def test_duplicate_workers_consume_one_revision(self):
        self.ingest()
        run = self.submit()
        result = self.race(
            lambda _: WorkflowExecutorService.execute(
                run.company_id, str(run.uuid), run.revision
            )
        )
        self.assertEqual(result.count(True), 1)

    def test_concurrent_retests_same_key_create_one_result_set(self):
        self.review()
        revision = self.audit.revision
        result = self.race(
            lambda _: service.execute(
                self.admin, self.audit, revision=revision, idempotency_key="same-retest"
            ).pk
        )
        self.assertEqual(result[0], result[1])
        self.assertEqual(self.audit.test_runs.count(), 2)

    def test_double_approval_has_one_winner(self):
        run = self.review()
        WorkflowEngineService.command(
            self.admin,
            run,
            action="accept",
            expected_revision=run.revision,
            idempotency_key="accept",
        )
        self.advance(run)
        revision = run.revision
        result = self.race(
            lambda i: WorkflowEngineService.command(
                self.admin,
                run,
                action="approve",
                expected_revision=revision,
                idempotency_key="approve-" + str(i),
            ).pk
        )
        self.assertEqual(result.count("denied"), 1)
        self.advance(run)
        self.assertEqual(self.audit.outputs.count(), 1)

    def test_concurrent_finalization_returns_one_version(self):
        run = self.review()
        WorkflowEngineService.command(
            self.admin,
            run,
            action="accept",
            expected_revision=run.revision,
            idempotency_key="accept",
        )
        self.advance(run)
        WorkflowEngineService.command(
            self.admin,
            run,
            action="approve",
            expected_revision=run.revision,
            idempotency_key="approve",
        )
        self.audit.refresh_from_db()
        result = self.race(lambda _: service.assemble(self.audit).pk)
        self.assertEqual(result[0], result[1])
        self.assertEqual(self.audit.outputs.count(), 1)
