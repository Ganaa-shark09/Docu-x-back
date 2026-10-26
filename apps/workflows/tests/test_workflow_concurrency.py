import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TransactionTestCase
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.approvals.models import ApprovalRequest
from apps.approvals.services import ApprovalWorkflowService
from apps.audit.models import AuditLog
from apps.documents.models import Document
from apps.tenants.models import Company
from apps.workflows.models import WorkflowRun
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowEngineService,
    WorkflowConflict,
)
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService


class WorkflowConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Concurrent", slug="concurrent")
        self.admin = User.objects.create_user(
            username="concurrent-admin", company=self.company, role="company_admin"
        )
        self.document = Document.objects.create(
            company=self.company,
            uploaded_by=self.admin,
            title="Concurrency",
            original_filename="input.txt",
            status="ready",
            is_ai_ready=True,
        )
        self.enqueue_patch = patch.object(WorkflowEngineService, "enqueue")
        self.enqueue_patch.start()
        self.addCleanup(self.enqueue_patch.stop)
        definition = WorkflowDefinitionService.create(
            self.admin, name="Concurrent", code="concurrent"
        )
        self.version = WorkflowDefinitionService.publish(
            self.admin,
            definition,
            entry_step="approval",
            steps=[
                {
                    "key": "approval",
                    "name": "Approve",
                    "step_type": "approval",
                    "configuration": {"assignment": {"user": self.admin.pk}},
                },
                {"key": "done", "name": "Done", "step_type": "terminal"},
                {
                    "key": "rejected",
                    "name": "Rejected",
                    "step_type": "terminal",
                    "configuration": {"status": "rejected"},
                },
            ],
            transitions=[
                {"source": "approval", "target": "done", "outcome": "approved"},
                {"source": "approval", "target": "rejected", "outcome": "rejected"},
            ],
        )
        self.run, _ = WorkflowEngineService.start(
            self.admin,
            version_uuid=self.version.uuid,
            document_uuids=[str(self.document.uuid)],
            idempotency_key="concurrent-run",
        )

    def race(self, operation):
        barrier = threading.Barrier(2)

        def execute(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation(index)
            except (ValidationError, WorkflowConflict):
                return "denied"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(execute, [0, 1]))

    def test_duplicate_workers_create_one_approval(self):
        result = self.race(
            lambda _: WorkflowExecutorService.execute(
                self.company.pk, str(self.run.uuid), self.run.revision
            )
        )
        self.assertEqual(sorted(result), [False, True])
        self.assertEqual(
            ApprovalRequest.objects.filter(company=self.company).count(), 1
        )
        self.assertEqual(self.run.step_runs.get(step=self.run.current_step).attempt, 1)

    def test_simultaneous_approval_rejection_progresses_once(self):
        WorkflowExecutorService.execute(
            self.company.pk, str(self.run.uuid), self.run.revision
        )
        approval_id = self.run.step_runs.get(step=self.run.current_step).approval_id

        def decide(index):
            actor = User.objects.get(pk=self.admin.pk)
            approval = ApprovalRequest.objects.get(pk=approval_id)
            if index:
                ApprovalWorkflowService.reject(
                    actor, approval, reviewer_notes="Rejected."
                )
            else:
                ApprovalWorkflowService.approve(actor, approval)
            return "decided"

        self.assertCountEqual(self.race(decide), ["decided", "denied"])
        self.assertEqual(
            AuditLog.objects.filter(
                resource_uuid=self.run.uuid, metadata__event="step_completed"
            ).count(),
            1,
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "queued")

    def test_duplicate_start_is_one_run(self):
        def start(_):
            run, _ = WorkflowEngineService.start(
                User.objects.get(pk=self.admin.pk),
                version_uuid=self.version.uuid,
                document_uuids=[str(self.document.uuid)],
                idempotency_key="concurrent-second",
            )
            return run.pk

        ids = self.race(start)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(
            WorkflowRun.objects.filter(
                company=self.company, idempotency_key="concurrent-second"
            ).count(),
            1,
        )

    def test_simultaneous_commands_require_current_revision(self):
        WorkflowExecutorService.execute(
            self.company.pk, str(self.run.uuid), self.run.revision
        )
        self.run.refresh_from_db()
        revision = self.run.revision

        def cancel(_):
            WorkflowEngineService.command(
                User.objects.get(pk=self.admin.pk),
                WorkflowRun.objects.get(pk=self.run.pk),
                action="cancel",
                expected_revision=revision,
                idempotency_key=uuid.uuid4().hex,
            )
            return "cancelled"

        self.assertCountEqual(self.race(cancel), ["cancelled", "denied"])
