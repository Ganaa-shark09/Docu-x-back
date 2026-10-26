import uuid

from django.core.files.base import ContentFile
from django.test import override_settings
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APITestCase, APIClient

from apps.documents.models import Document
from apps.workflows.services.compliance_library_service import (
    ComplianceLibraryService as library,
)
from apps.workflows.services.compliance_assessment_service import (
    ComplianceAssessmentService as service,
)
from apps.workflows.services.compliance_evidence_service import (
    ComplianceEvidenceService as evidence,
)
from apps.workflows.tests.test_compliance_workflow import FRAMEWORK


@override_settings(MEDIA_ROOT="/tmp/docux-compliance-integrations")
class ComplianceIntegrationTests(APITestCase):
    def source_fixture(self, base):
        # Reuse the accepted domain's real workflow fixture and processing pipeline.
        fixture_class = type("SourceFixture", (base,), {})
        fixture_class.setUpTestData()
        fixture = fixture_class(methodName="runTest")
        fixture.client = APIClient()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def assessment(self, fixture, documents):
        for doc in documents:
            if not doc.file:
                text = getattr(doc, "text_extraction", None)
                doc.file.save(
                    str(uuid.uuid4()) + ".txt",
                    ContentFile((text.extracted_text if text else "Source").encode()),
                )
                self.addCleanup(lambda d=doc: d.file.delete(save=False))
        framework = library.create_framework(
            fixture.admin,
            code="compliance-integration",
            name="Generic internal review",
            draft=FRAMEWORK,
        )
        version = library.publish(fixture.admin, framework, revision=1)
        control = library.create_control(
            fixture.admin,
            code="support",
            name="Source review",
            department=fixture.department.pk,
            definition={
                "objective": "Assess the reviewed source",
                "owner": {"user": fixture.admin.pk},
            },
        )
        requirement = version.requirements.get()
        library.map_control(
            fixture.admin, control, revision=1, requirement_uuid=requirement.uuid
        )
        assessment = service.create(
            fixture.admin,
            title="Evidence integration",
            framework_version_uuid=version.uuid,
            department=fixture.department.pk,
            scope={
                "start": "2026-01-01",
                "end": "2026-12-31",
                "document_uuids": [str(d.uuid) for d in documents],
            },
            idempotency_key="integration",
        )
        return assessment, requirement, control

    def link(self, fixture, assessment, requirement, control, kind, source):
        return service.link(
            fixture.admin,
            assessment,
            revision=1,
            requirement_uuid=requirement.uuid,
            control_uuid=control.uuid,
            source_kind=kind,
            source_uuid=source.uuid,
            idempotency_key="evidence",
        )

    def test_finance_output_is_pinned_authorized_and_not_copied(self):
        from apps.workflows.tests.test_finance_audit import FinanceAuditTests

        fixture = self.source_fixture(FinanceAuditTests)
        fixture.test_approval_and_versioned_output()
        output = fixture.audit.outputs.get()
        assessment, req, ctrl = self.assessment(
            fixture, list(fixture.audit.run.documents.all())
        )
        link = self.link(fixture, assessment, req, ctrl, "finance_output", output)
        self.assertEqual(link.source_uuid, output.uuid)
        self.assertEqual(link.pin["domain_uuid"], str(fixture.audit.uuid))
        self.assertNotIn("dataset_snapshots", link.pin)
        doc = fixture.audit.run.documents.first()
        Document.objects.filter(pk=doc.pk).update(governance_revision=999)
        with self.assertRaises(PermissionDenied):
            service.authorize(fixture.admin, assessment)

    def test_hr_policy_keeps_exact_version_and_retirement_makes_stale(self):
        from apps.workflows.tests.test_hr_policy_workflow import HRPolicyWorkflowTests
        from apps.workflows.models.hr_policy import HRPolicyPublication

        fixture = self.source_fixture(HRPolicyWorkflowTests)
        version = fixture.published()
        assessment, req, ctrl = self.assessment(fixture, [version.document])
        link = self.link(fixture, assessment, req, ctrl, "hr_policy_version", version)
        self.assertEqual(link.pin["version"], version.number)
        self.assertEqual(link.source_uuid, version.uuid)
        HRPolicyPublication.objects.filter(version=version).update(status="retired")
        self.assertEqual(
            evidence.validity(fixture.admin, assessment, link, True), "stale"
        )
        link.refresh_from_db()
        self.assertEqual(link.source_uuid, version.uuid)

    def test_contract_output_reuses_existing_extraction(self):
        from apps.workflows.tests.test_contract_workflow import ContractWorkflowTests
        from apps.workflows.models import ExtractionRun

        fixture = self.source_fixture(ContractWorkflowTests)
        run = fixture.advance(fixture.start())
        self.assertEqual(run.status, "completed")
        output = run.contract_review.outputs.get()
        before = ExtractionRun.objects.count()
        assessment, req, ctrl = self.assessment(fixture, [fixture.document])
        link = self.link(fixture, assessment, req, ctrl, "contract_output", output)
        self.assertEqual(link.pin["revision"], output.revision)
        self.assertEqual(ExtractionRun.objects.count(), before)

    def test_legal_output_reuses_reviewed_clause_analysis(self):
        from apps.workflows.tests.test_clause_intelligence import (
            ClauseIntelligenceTests,
        )
        from apps.workflows.models import ClauseAnalysis

        fixture = self.source_fixture(ClauseIntelligenceTests)
        run = fixture.advance(fixture.start())
        fixture.review(run)
        fixture.accept(run)
        fixture.advance(run)
        self.assertEqual(run.status, "completed")
        before = ClauseAnalysis.objects.count()
        assessment, req, ctrl = self.assessment(fixture, [fixture.document])
        output = run.clause_analysis.outputs.get()
        link = self.link(fixture, assessment, req, ctrl, "clause_output", output)
        self.assertEqual(link.source_uuid, output.uuid)
        self.assertEqual(ClauseAnalysis.objects.count(), before)

    def test_rfp_claim_is_pending_review_and_requires_explicit_knowledge_scope(self):
        from apps.workflows.tests.test_rfp_workflow import RFPWorkflowTests

        fixture = self.source_fixture(RFPWorkflowTests)
        output = fixture.finalize()
        fixture.doc = fixture.project.sources.get().document
        assessment, req, ctrl = self.assessment(fixture, [fixture.doc])
        artifact = fixture.project.run.artifacts.first()
        self.assertIsNotNone(artifact)
        with self.assertRaises(PermissionDenied):
            self.link(fixture, assessment, req, ctrl, "workflow_artifact", artifact)
        with self.assertRaises(PermissionDenied):
            self.link(fixture, assessment, req, ctrl, "rfp_output", output)
        # A new explicit scope must contain the approved proposal's knowledge basis.
        fixture.knowledge.file.save(
            str(uuid.uuid4()) + ".txt",
            ContentFile(fixture.knowledge.text_extraction.extracted_text.encode()),
        )
        self.addCleanup(lambda: fixture.knowledge.file.delete(save=False))
        scope = {
            **assessment.scope,
            "document_uuids": [str(fixture.doc.uuid), str(fixture.knowledge.uuid)],
        }
        assessment = service.create(
            fixture.admin,
            title="Scoped proposal evidence",
            framework_version_uuid=assessment.framework_version.uuid,
            department=fixture.department.pk,
            scope=scope,
            idempotency_key="with-knowledge",
        )
        link = self.link(fixture, assessment, req, ctrl, "rfp_output", output)
        self.assertEqual(
            evidence.validity(fixture.admin, assessment, link), "pending_review"
        )
        self.assertFalse(assessment.evaluations.exists())
