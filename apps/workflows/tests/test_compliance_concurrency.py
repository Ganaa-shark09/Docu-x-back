import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.workflows.services.compliance_assessment_service import (
    ComplianceAssessmentService as service,
)
from apps.workflows.services.compliance_library_service import (
    ComplianceLibraryService as library,
)
from apps.workflows.services.compliance_workflow_service import (
    ComplianceWorkflowService,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
)
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService
from apps.workflows.tests import test_compliance_workflow as fixtures


@override_settings(MEDIA_ROOT="/tmp/docux-compliance-concurrency")
class ComplianceConcurrencyTests(TransactionTestCase):
    document = fixtures.ComplianceWorkflowTests.document
    create = fixtures.ComplianceWorkflowTests.create
    link = fixtures.ComplianceWorkflowTests.link
    advance = fixtures.ComplianceWorkflowTests.advance
    review = fixtures.ComplianceWorkflowTests.review
    decision = fixtures.ComplianceWorkflowTests.decision
    evaluate = fixtures.ComplianceWorkflowTests.evaluate
    satisfied = fixtures.ComplianceWorkflowTests.satisfied
    finish = fixtures.ComplianceWorkflowTests.finish

    def setUp(self):
        fixtures.ComplianceWorkflowTests.setUpTestData.__func__(type(self))
        self.client = APIClient()
        fixtures.ComplianceWorkflowTests.setUp(self)

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

    def test_framework_publication_one_revision_one_winner(self):
        self.framework.refresh_from_db()
        revision = self.framework.revision
        result = self.race(
            lambda _: library.publish(self.admin, self.framework, revision=revision).pk
        )
        self.assertEqual(result.count("denied"), 1)
        self.assertEqual(self.framework.versions.count(), 2)

    def test_mapping_serializes_revision(self):
        self.control.refresh_from_db()
        revision = self.control.revision
        result = self.race(
            lambda _: library.map_control(
                self.admin,
                self.control,
                revision=revision,
                requirement_uuid=self.req.uuid,
            ).pk
        )
        self.assertEqual(result.count("denied"), 1)
        self.assertEqual(self.control.mappings.count(), 1)

    def test_evidence_collection_duplicate_key(self):
        values = dict(
            revision=1,
            requirement_uuid=self.req.uuid,
            control_uuid=self.control.uuid,
            source_kind="document",
            source_uuid=self.doc.uuid,
            idempotency_key="same",
        )
        result = self.race(
            lambda _: service.link(self.admin, self.assessment, **values).pk
        )
        self.assertEqual(result[0], result[1])
        self.assertEqual(self.assessment.evidence.count(), 1)

    def test_workflow_start_duplicate_delivery(self):
        result = self.race(
            lambda _: ComplianceWorkflowService.submit(
                self.admin,
                self.assessment,
                revision=1,
                reviewer={"user": self.admin.pk},
                approver={"user": self.admin.pk},
                idempotency_key="same",
            ).pk
        )
        self.assertEqual(result[0], result[1])

    def test_worker_duplicate_delivery(self):
        run = ComplianceWorkflowService.submit(
            self.admin,
            self.assessment,
            revision=1,
            reviewer={"user": self.admin.pk},
            approver={"user": self.admin.pk},
            idempotency_key="same",
        )
        revision = run.revision
        result = self.race(
            lambda _: WorkflowExecutorService.execute(
                run.company_id, str(run.uuid), revision
            )
        )
        self.assertEqual(result.count(True), 1)

    def decision_race(self, kind, target, outcome, data=None):
        self.assessment.refresh_from_db()
        revision = self.assessment.revision
        return self.race(
            lambda i: service.decide(
                self.admin,
                self.assessment,
                revision=revision,
                kind=kind,
                target=target,
                outcome=outcome,
                reason="Concurrent review",
                data=data or {},
                idempotency_key="concurrent-" + str(i),
            ).pk
        )

    def test_evidence_review_revision_safe(self):
        _, proof = self.review(True)
        self.assertEqual(
            self.decision_race("evidence", str(proof.uuid), "valid").count("denied"), 1
        )

    def test_control_review_revision_safe(self):
        self.review()
        target = self.assessment.evaluations.get().data["rows"][0]["target"]
        self.assertEqual(
            self.decision_race("control", target, "insufficient_evidence").count(
                "denied"
            ),
            1,
        )

    def test_risk_acceptance_revision_safe(self):
        self.review()
        target = self.assessment.evaluations.get().data["rows"][0]["target"]
        self.assertEqual(
            self.decision_race(
                "risk_acceptance", target, "accepted_risk", {"expires": "2099-01-01"}
            ).count("denied"),
            1,
        )

    def test_remediation_revision_safe(self):
        self.review()
        target = self.assessment.evaluations.get().data["rows"][0]["target"]
        self.assertEqual(
            self.decision_race(
                "remediation",
                target,
                "open",
                {"owner": {"user": self.admin.pk}, "due": "2099-01-01"},
            ).count("denied"),
            1,
        )

    def test_evaluation_duplicate_key_preserves_one_result_set(self):
        self.review()
        self.assessment.refresh_from_db()
        revision = self.assessment.revision
        result = self.race(
            lambda _: service.evaluate(
                self.admin, self.assessment, revision=revision, idempotency_key="same"
            ).pk
        )
        self.assertEqual(result[0], result[1])
        self.assertEqual(self.assessment.evaluations.count(), 2)

    def test_double_approval_progresses_once(self):
        run = self.satisfied()
        run.refresh_from_db()
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
        self.assertEqual(self.assessment.outputs.count(), 1)

    def test_concurrent_finalization_produces_one_output(self):
        run = self.satisfied()
        run.refresh_from_db()
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
        self.assessment.refresh_from_db()
        result = self.race(lambda _: service.assemble(self.assessment).pk)
        self.assertEqual(result[0], result[1])
        self.assertEqual(self.assessment.outputs.count(), 1)

    def test_reassessment_duplicate_command_has_one_child(self):
        self.finish(self.satisfied())
        result = self.race(
            lambda _: self.create(
                "new-assessment", previous_uuid=self.assessment.uuid
            ).pk
        )
        self.assertEqual(result[0], result[1])
        self.assertEqual(self.assessment.reassessments.count(), 1)
