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
from apps.workflows.models import (
    ClauseType,
    ClauseAnalysis,
    ClausePlaybook,
    WorkflowRun,
)
from apps.workflows.services.clause_playbook_service import ClausePlaybookService
from apps.workflows.services.clause_analysis_service import ClauseAnalysisService
from apps.workflows.services.clause_definition_service import ClauseDefinitionService
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
)
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService
from apps.workflows.tests.test_clause_intelligence import TEXT


class ClauseConcurrencyTests(TransactionTestCase):
    def setUp(self):
        importlib.import_module(
            "apps.workflows.migrations.0010_seed_clause_taxonomy"
        ).seed(apps, None)
        self.company = Company.objects.create(
            name="Concurrent Legal", slug="concurrent-legal"
        )
        self.admin = User.objects.create_user(
            username="concurrent-legal", company=self.company, role="company_admin"
        )
        self.document = Document.objects.create(
            company=self.company,
            uploaded_by=self.admin,
            title="Contract",
            original_filename="contract.txt",
            document_type="contract",
            status="ready",
            is_ai_ready=True,
        )
        DocumentTextExtraction.objects.create(
            document=self.document,
            extracted_text=TEXT,
            extraction_method="plain_text",
            page_count=1,
            confidence_score=100,
        )
        cap = ClauseType.objects.get(company=None, code="limitation_of_liability")
        self.book = ClausePlaybookService.create(
            self.admin,
            name="Standard",
            draft={
                "rules": [
                    {
                        "code": "cap",
                        "clause_type": str(cap.uuid),
                        "requirement": "required",
                    }
                ]
            },
        )
        self.playbook = ClausePlaybookService.publish(self.admin, self.book, 1)
        self.book.refresh_from_db()
        self.book = ClausePlaybookService.activate(
            self.admin, self.book, self.book.revision, version=self.playbook
        )
        self.version = ClauseDefinitionService.create(
            self.admin,
            name="Legal",
            code="legal",
            reviewer={"user": self.admin.pk},
            policy={"playbook_version": str(self.playbook.uuid)},
        )[1]
        self.enqueue = patch.object(WorkflowEngineService, "enqueue")
        self.enqueue.start()
        self.addCleanup(self.enqueue.stop)
        self.run = WorkflowEngineService.start(
            self.admin,
            version_uuid=self.version.uuid,
            document_uuids=[str(self.document.uuid)],
            idempotency_key="original",
        )[0]

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
        for _ in range(20):
            self.run.refresh_from_db()
            if self.run.status != "queued" or self.run.current_step.key == until:
                return
            WorkflowExecutorService.execute(
                self.company.pk, str(self.run.uuid), self.run.revision
            )
        self.fail("Unbounded legal processing")

    def test_duplicate_workers_create_one_analysis(self):
        revision = self.run.revision
        self.assertCountEqual(
            self.race(
                lambda _: WorkflowExecutorService.execute(
                    self.company.pk, str(self.run.uuid), revision
                )
            ),
            [True, False],
        )
        self.assertEqual(
            ClauseAnalysis.objects.filter(workflow_run=self.run).count(), 1
        )
        self.assertEqual(self.run.clause_analysis.clauses.count(), 2)

    def test_competing_publications_preserve_single_version(self):
        revision = self.book.revision

        def publish(_):
            ClausePlaybookService.publish(
                User.objects.get(pk=self.admin.pk),
                ClausePlaybook.objects.get(pk=self.book.pk),
                revision,
            )
            return "published"

        self.assertCountEqual(self.race(publish), ["published", "denied"])
        self.assertEqual(self.book.versions.count(), 2)

    def test_competing_review_decisions_are_revision_safe(self):
        self.advance()
        analysis = self.run.clause_analysis
        target = analysis.clauses.first().uuid
        revision = self.run.revision

        def decide(index):
            ClauseAnalysisService.decide(
                User.objects.get(pk=self.admin.pk),
                ClauseAnalysis.objects.get(pk=analysis.pk),
                target=target,
                target_type="clause",
                decision="confirmed",
                reason="Verified source",
                expected_revision=revision,
                idempotency_key=f"review-{index}",
            )
            return "reviewed"

        self.assertCountEqual(self.race(decide), ["reviewed", "denied"])
        self.assertEqual(analysis.decisions.count(), 1)

    def test_same_review_key_is_idempotent_under_concurrency(self):
        self.advance()
        analysis = self.run.clause_analysis
        target = analysis.clauses.first().uuid
        revision = self.run.revision

        def decide(_):
            return str(
                ClauseAnalysisService.decide(
                    User.objects.get(pk=self.admin.pk),
                    ClauseAnalysis.objects.get(pk=analysis.pk),
                    target=target,
                    target_type="clause",
                    decision="confirmed",
                    reason="Verified source",
                    expected_revision=revision,
                    idempotency_key="same-review",
                ).uuid
            )

        results = self.race(decide)
        self.assertEqual(results[0], results[1])
        self.assertEqual(analysis.decisions.count(), 1)

    def test_duplicate_finalization_workers_create_one_output(self):
        self.advance()
        analysis = self.run.clause_analysis
        for clause in analysis.clauses.all():
            self.run.refresh_from_db()
            ClauseAnalysisService.decide(
                self.admin,
                analysis,
                target=clause.uuid,
                target_type="clause",
                decision="confirmed",
                reason="Verified source",
                expected_revision=self.run.revision,
                idempotency_key="review-" + str(clause.uuid),
            )
        self.run.refresh_from_db()
        WorkflowEngineService.command(
            self.admin,
            self.run,
            action="accept",
            expected_revision=self.run.revision,
            idempotency_key="accept",
        )
        self.advance("finalize_clause_analysis")
        revision = self.run.revision
        self.assertCountEqual(
            self.race(
                lambda _: WorkflowExecutorService.execute(
                    self.company.pk, str(self.run.uuid), revision
                )
            ),
            [True, False],
        )
        self.assertEqual(analysis.outputs.count(), 1)

    def test_same_start_key_returns_one_run(self):
        def start(_):
            run, created = WorkflowEngineService.start(
                User.objects.get(pk=self.admin.pk),
                version_uuid=self.version.uuid,
                document_uuids=[str(self.document.uuid)],
                idempotency_key="parallel",
            )
            return str(run.uuid), created

        results = self.race(start)
        self.assertEqual(results[0][0], results[1][0])
        self.assertCountEqual([r[1] for r in results], [True, False])
        self.assertEqual(WorkflowRun.objects.count(), 2)

    def test_competing_corrections_preserve_single_revision_and_original(self):
        self.advance()
        analysis = self.run.clause_analysis
        clause = analysis.clauses.first()
        revision = self.run.revision

        def correct(index):
            ClauseAnalysisService.correct(
                User.objects.get(pk=self.admin.pk),
                ClauseAnalysis.objects.get(pk=analysis.pk),
                clause=clause.uuid,
                clause_type=clause.clause_type.uuid,
                facts=clause.facts,
                evidence=clause.evidence,
                reason=f"Source verification {index}",
                expected_revision=revision,
            )
            return "corrected"

        self.assertCountEqual(self.race(correct), ["corrected", "denied"])
        analysis.refresh_from_db()
        self.assertEqual(analysis.revision, 2)
        self.assertEqual(clause.corrections.count(), 1)
        self.assertEqual(clause.corrections.get().facts, clause.facts)
