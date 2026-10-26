import uuid

from django.core.files.base import ContentFile
from django.test import override_settings
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APITestCase, APIClient

from apps.documents.models import Document
from apps.workflows.services.proof_normalization_service import (
    ProofNormalizationService as normalize,
)
from apps.workflows.services.proof_source_service import ProofSourceService
from apps.workflows.services.source_proof_service import SourceProofService as proof


@override_settings(MEDIA_ROOT="/tmp/docux-proof-integration-tests")
class SourceProofIntegrationTests(APITestCase):
    def fixture(self, base):
        cls = type("ProofFixture", (base,), {})
        cls.setUpTestData()
        instance = cls(methodName="runTest")
        instance.client = APIClient()
        instance.setUp()
        self.addCleanup(instance.doCleanups)
        return instance

    def files(self, docs):
        for doc in docs:
            if not doc.file:
                extraction = getattr(doc, "text_extraction", None)
                doc.file.save(
                    str(uuid.uuid4()) + ".txt",
                    ContentFile(
                        (extraction.extracted_text if extraction else "Source").encode()
                    ),
                )
                self.addCleanup(lambda d=doc: d.file.delete(save=False))

    def check(self, fixture, kind, output, path):
        claim = normalize.output(fixture.admin, kind, output.uuid, path)
        again = normalize.output(fixture.admin, kind, output.uuid, path)
        self.assertEqual(claim.pk, again.pk)
        result = proof.graph(ProofSourceService(fixture.admin), claim)
        self.assertTrue(result["evidence"])
        self.assertEqual(result["evidence"][0]["source"]["kind"], kind)
        with self.assertRaises(PermissionDenied):
            proof.graph(ProofSourceService(fixture.foreign), claim)
        return claim, result

    def test_invoice_field_and_line_item_proof(self):
        from apps.workflows.tests.test_invoice_workflow import InvoiceWorkflowTests

        f = self.fixture(InvoiceWorkflowTests)
        self.files([f.document])
        run = f.advance(f.start())
        self.assertEqual(run.status, "completed")
        output = run.invoice.outputs.get()
        response = f.client.get(
            "/api/workflows/proof-claims/context/", {"run_uuid": run.uuid}
        )
        self.assertEqual(response.status_code, 200, response.data)
        fields = response.data["output_fields"]
        self.assertTrue(
            any(item["path"] == ["invoice", "grand_total"] for item in fields)
        )
        self.assertLessEqual(len(fields), 100)
        self.assertTrue(all(item["source_uuid"] == str(output.uuid) for item in fields))
        _, result = self.check(f, "invoice_output", output, ["invoice", "grand_total"])
        self.assertTrue(any(e["precision"] == "exact" for e in result["evidence"]))
        self.check(f, "invoice_output", output, ["invoice", "line_items"])

    def test_contract_fact_and_boundary_proof(self):
        from apps.workflows.tests.test_contract_workflow import ContractWorkflowTests

        f = self.fixture(ContractWorkflowTests)
        self.files([f.document])
        run = f.advance(f.start())
        self.assertEqual(run.status, "completed")
        _, result = self.check(
            f,
            "contract_output",
            run.contract_review.outputs.get(),
            ["contract", "renewal_term"],
        )
        self.assertTrue(
            any(e["locator_type"] == "text_offsets" for e in result["evidence"])
        )

    def test_invoice_native_correction_lineage_is_imported_without_overwrite(self):
        from apps.workflows.tests.test_invoice_workflow import InvoiceWorkflowTests
        from apps.workflows.models import InvoiceOutputVersion

        f = self.fixture(InvoiceWorkflowTests)
        self.files([f.document])
        f.test_missing_field_requires_correction_and_preserves_history()
        output = InvoiceOutputVersion.objects.get(invoice__company=f.company)
        _, result = self.check(f, "invoice_output", output, ["invoice", "grand_total"])
        corrections = [v for v in result["claims"] if v["review_state"] == "corrected"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["value"], "110.00")
        self.assertEqual(corrections[0]["actor"], f.reviewer.pk)
        self.assertEqual(corrections[0]["reason"], "Verified against source")
        self.assertTrue(any(v["value"] == "" for v in result["claims"]))

    def test_legacy_invoice_output_does_not_gain_fabricated_source_version(self):
        from apps.workflows.tests.test_invoice_workflow import InvoiceWorkflowTests
        from apps.workflows.models import InvoiceOutputVersion

        f = self.fixture(InvoiceWorkflowTests)
        self.files([f.document])
        run = f.advance(f.start())
        output = run.invoice.outputs.get()
        InvoiceOutputVersion.objects.filter(pk=output.pk).update(source_pins=[])
        _, result = self.check(f, "invoice_output", output, ["invoice", "grand_total"])
        self.assertTrue(
            all(e["code"] == "legacy_unverified" for e in result["evidence"])
        )

    def test_invoice_file_tampering_before_first_normalization_is_denied(self):
        from apps.workflows.tests.test_invoice_workflow import InvoiceWorkflowTests

        f = self.fixture(InvoiceWorkflowTests)
        self.files([f.document])
        run = f.advance(f.start())
        with f.document.file.open("wb") as stream:
            stream.write(b"changed after output")
        with self.assertRaises(PermissionDenied):
            normalize.output(
                f.admin,
                "invoice_output",
                run.invoice.outputs.get().uuid,
                ["invoice", "grand_total"],
            )

    def test_legal_fact_policy_deviation_are_separate_paths(self):
        from apps.workflows.tests.test_clause_intelligence import (
            ClauseIntelligenceTests,
        )

        f = self.fixture(ClauseIntelligenceTests)
        self.files([f.document])
        run = f.advance(f.start())
        f.review(run)
        f.accept(run)
        f.advance(run)
        output = run.clause_analysis.outputs.get()
        for field in ["clauses", "deviations"]:
            self.check(f, "clause_output", output, [field])

    def test_hr_exact_version_and_retired_status(self):
        from apps.workflows.tests.test_hr_policy_workflow import HRPolicyWorkflowTests
        from apps.workflows.models import HRPolicyPublication

        f = self.fixture(HRPolicyWorkflowTests)
        version = f.published()
        self.files([version.document])
        claim, result = self.check(f, "hr_policy_version", version, ["number"])
        self.assertEqual(
            result["evidence"][0]["source"]["reference"], str(version.uuid)
        )
        HRPolicyPublication.objects.filter(version=version).update(status="retired")
        result = proof.graph(ProofSourceService(f.admin), claim)
        self.assertTrue(all(e["validity"] == "stale" for e in result["evidence"]))

    def test_rfp_approved_response_transitive_knowledge_and_draft_rejected(self):
        from apps.workflows.tests.test_rfp_workflow import RFPWorkflowTests
        from apps.workflows.models import RFPResponseVersion

        f = self.fixture(RFPWorkflowTests)
        self.files([f.project.sources.get().document, f.knowledge])
        output = f.finalize()
        self.files([f.project.sources.get().document, f.knowledge])
        claim, _ = self.check(f, "rfp_output", output, ["responses"])
        source = output.data["responses"][0]["response_uuid"]
        response = RFPResponseVersion.objects.get(uuid=source)
        self.check(f, "rfp_response", response, ["text"])
        draft = RFPResponseVersion.objects.create(
            item=response.item,
            number=99,
            text="Unsupported draft",
            origin="human",
            actor=f.admin,
        )
        with self.assertRaises(PermissionDenied):
            normalize.output(f.admin, "rfp_response", draft.uuid, ["text"])
        Document.objects.filter(pk=f.knowledge.pk).update(is_deleted=True)
        with self.assertRaises(PermissionDenied):
            proof.graph(ProofSourceService(f.admin), claim)

    def test_finance_dataset_transaction_and_decimal_proof(self):
        from apps.workflows.tests.test_finance_audit import FinanceAuditTests

        f = self.fixture(FinanceAuditTests)
        dataset = f.ingest()
        row = dataset.transactions.get()
        evidence = proof.capture(
            f.admin,
            "finance_dataset",
            dataset.uuid,
            "dataset_row",
            {"transaction_uuid": str(row.uuid)},
            "structured_record",
        )
        self.assertEqual(evidence.locator["row"], row.row)
        claim, result = self.check(f, "finance_transaction", row, ["amount"])
        self.assertEqual(result["claims"][0]["value"], "100.01000000")
        with self.assertRaises(ValidationError):
            proof.capture(
                f.admin,
                "finance_dataset",
                dataset.uuid,
                "dataset_row",
                {"transaction_uuid": str(uuid.uuid4())},
                "structured_record",
            )
        value = claim.versions.get()
        derived = proof.derive(
            f.admin, evidence, "double_amount", "sum", [value, value]
        )
        result = proof.graph(ProofSourceService(f.admin), derived)
        self.assertEqual(result["claims"][0]["value"], "200.02000000")
        self.assertEqual(result["claims"][0]["derivation"]["operation"], "calculation")

    def test_finance_output_keeps_anomaly_and_assurance_semantics(self):
        from apps.workflows.tests.test_finance_audit import FinanceAuditTests

        f = self.fixture(FinanceAuditTests)
        f.test_approval_and_versioned_output()
        output = f.audit.outputs.get()
        self.check(f, "finance_output", output, ["assistance_only"])

    def test_compliance_framework_control_and_risk_history_proof(self):
        from apps.workflows.tests.test_compliance_workflow import (
            ComplianceWorkflowTests,
        )

        f = self.fixture(ComplianceWorkflowTests)
        run = f.satisfied()
        output = f.finish(run)
        _, result = self.check(f, "compliance_output", output, ["rows"])
        self.assertEqual(
            result["evidence"][0]["source"]["pin"]["version"]["framework_version_uuid"],
            str(f.version.uuid),
        )

    def test_compliance_date_only_review_due_remains_valid_through_that_day(self):
        from django.utils import timezone
        from apps.workflows.tests.test_compliance_workflow import (
            ComplianceWorkflowTests,
        )

        f = self.fixture(ComplianceWorkflowTests)
        run = f.satisfied(dates={"review_due": timezone.localdate().isoformat()})
        output = f.finish(run)
        _, result = self.check(f, "compliance_output", output, ["rows"])
        self.assertFalse(any(e["validity"] == "stale" for e in result["evidence"]))

    def test_finance_authorization_is_not_repeated_for_each_input_row(self):
        from unittest.mock import patch
        from apps.workflows.tests.test_finance_audit import FinanceAuditTests, RECORD
        from apps.workflows.services.finance_audit_service import FinanceAuditService

        f = self.fixture(FinanceAuditTests)
        dataset = f.ingest(rows=[RECORD, RECORD])
        resolver = ProofSourceService(f.admin)
        with patch.object(
            FinanceAuditService, "authorize", wraps=FinanceAuditService.authorize
        ) as checked:
            for row in dataset.transactions.all():
                resolver.resolve("finance_transaction", row.uuid)
        self.assertEqual(checked.call_count, 1)
