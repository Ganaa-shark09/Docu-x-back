import json
import uuid
from unittest.mock import patch

from django.test import override_settings
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APITestCase

from apps.document_processing.models import DocumentTextExtraction
from apps.documents.models import DocumentFieldPolicy
from apps.workflows.models import (
    InvoiceRecord,
    InvoiceVendor,
    WorkflowDefinition,
    WorkflowVersion,
    WorkflowRun,
    WorkflowRunDocument,
)
from apps.workflows.services.contract_extraction_service import evidence
from apps.workflows.services.finance_audit_service import FinanceAuditService as service
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
)
from apps.workflows.tests import test_finance_audit as fixtures

RECORD, SCOPE = fixtures.RECORD, fixtures.SCOPE


@override_settings(MEDIA_ROOT="/tmp/docux-finance-tests")
class FinanceReconciliationTests(APITestCase):
    setUpTestData = classmethod(fixtures.FinanceAuditTests.setUpTestData.__func__)
    setUp = fixtures.FinanceAuditTests.setUp
    source = fixtures.FinanceAuditTests.source
    ingest = fixtures.FinanceAuditTests.ingest
    advance = fixtures.FinanceAuditTests.advance
    submit = fixtures.FinanceAuditTests.submit
    review = fixtures.FinanceAuditTests.review

    def invoice(
        self,
        *,
        total="100.01",
        currency="USD",
        vendor="V1",
        company=None,
        date="2026-01-01",
    ):
        company = company or self.company
        actor = self.admin if company == self.company else self.foreign
        doc = self.source(company=None if company == self.company else company)
        definition = WorkflowDefinition.objects.create(
            company=company, name="Invoice fixture", code="invoice-" + uuid.uuid4().hex
        )
        version = WorkflowVersion.objects.create(
            definition=definition, number=1, entry_step="completed", published_by=actor
        )
        run = WorkflowRun.objects.create(
            company=company,
            version=version,
            initiated_by=actor,
            status="completed",
            idempotency_key=uuid.uuid4().hex,
            correlation_id=uuid.uuid4(),
            request_fingerprint="fixture",
        )
        WorkflowRunDocument.objects.create(
            run=run, document=doc, governance_revision=doc.governance_revision
        )
        return InvoiceRecord.objects.create(
            company=company,
            workflow_run=run,
            document=doc,
            invoice_number="INV1",
            normalized={
                "invoice_number": "INV1",
                "grand_total": total,
                "currency": currency,
                "vendor_identifier": vendor,
                "vendor_name": "Vendor",
                "vendor_tax_id": "",
                "invoice_date": date,
            },
        )

    def reconcile(self, **record):
        service.configure(
            self.admin,
            self.audit,
            revision=1,
            scope=SCOPE,
            policy={"tests": ["invoice", "vendor", "three_way", "duplicates"]},
        )
        InvoiceVendor.objects.create(
            company=self.company, identifier="V1", name="Vendor"
        )
        self.review(
            [
                {
                    **RECORD,
                    "transaction_type": "payment",
                    "invoice_reference": "INV1",
                    **record,
                }
            ]
        )
        return self.audit.test_runs.get()

    def proof(self, role, values, row=None):
        text = "\n".join(f"{key}: {value}" for key, value in values.items())
        doc = self.source(raw=text.encode())
        DocumentTextExtraction.objects.create(
            document=doc,
            extraction_method="plain_text",
            extracted_text=text,
            page_count=1,
            confidence_score=100,
        )
        self.audit.refresh_from_db()
        row = row or service.rows(self.audit).get()
        link = service.support(
            self.admin,
            self.audit,
            row.uuid,
            revision=self.audit.revision,
            document_uuid=doc.uuid,
            role=role,
            evidence={"proof": evidence(doc, text, 0, len(text)), "values": values},
        )
        return link

    def retest(self):
        self.audit.refresh_from_db()
        return service.execute(
            self.admin,
            self.audit,
            revision=self.audit.revision,
            idempotency_key=uuid.uuid4().hex,
        )

    def test_exact_invoice_match(self):
        self.invoice()
        result = self.reconcile().results.get(code="invoice")
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.evidence["match"]["status"], "matched")
        self.assertEqual(result.evidence["calculation"]["difference"], "0.00")

    def test_probable_match_is_distinct(self):
        self.invoice()
        result = self.reconcile(vendor_identifier="").results.get(code="invoice")
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.evidence["match"]["status"], "probable_match")

    def test_partial_payment(self):
        self.invoice()
        result = self.reconcile(gross_amount="50.01").results.get(code="invoice")
        self.assertEqual(result.evidence["reconciliation"], "partial_payment")
        self.assertEqual(result.evidence["calculation"]["difference"], "-50.00")

    def test_overpayment_finding(self):
        self.invoice()
        result = self.reconcile(gross_amount="150.01").results.get(code="invoice")
        self.assertEqual(result.evidence["reconciliation"], "overpayment")
        self.assertIsNotNone(result.finding_id)

    def test_fx_not_invented(self):
        self.invoice(currency="EUR")
        result = self.reconcile().results.get(code="invoice")
        self.assertEqual(
            result.evidence["reconciliation"], "currency_comparison_unavailable"
        )
        self.assertEqual(result.evidence["calculation"]["fx"], "unavailable")

    def test_foreign_invoice_never_matches(self):
        self.invoice(company=self.other)
        result = self.reconcile().results.get(code="invoice")
        self.assertEqual(result.evidence["match"]["status"], "not_found")

    def test_vendor_mismatch(self):
        self.invoice(vendor="V2")
        result = self.reconcile().results.get(code="invoice")
        self.assertTrue(result.evidence["match"]["vendor_mismatch"])
        self.assertEqual(result.status, "review_required")

    def test_ambiguous_invoice_never_silently_selected(self):
        self.invoice()
        self.invoice()
        result = self.reconcile().results.get(code="invoice")
        self.assertEqual(result.evidence["match"]["status"], "ambiguous")

    def test_payment_before_invoice_requires_review(self):
        self.invoice(date="2026-01-20")
        result = self.reconcile().results.get(code="invoice")
        self.assertTrue(result.evidence["payment_before_invoice"])
        self.assertEqual(result.status, "review_required")

    def test_invoice_revocation_denies_future_read(self):
        invoice = self.invoice()
        self.reconcile()
        invoice.document.is_deleted = True
        invoice.document.save(update_fields=["is_deleted"])
        with self.assertRaises(PermissionDenied):
            service.authorize(self.admin, self.audit)

    def test_normalized_invoice_revision_invalidates_match(self):
        invoice = self.invoice()
        self.reconcile()
        invoice.revision += 1
        invoice.save(update_fields=["revision"])
        with self.assertRaises(PermissionDenied):
            service.authorize(self.admin, self.audit)

    def test_missing_po_and_receipt_explicit(self):
        self.invoice()
        result = self.reconcile(purchase_order_reference="PO1").results.get(
            code="three_way"
        )
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(set(result.evidence["missing"]), {"purchase_order", "receipt"})

    def test_three_way_match_with_real_proof(self):
        self.invoice()
        self.reconcile(purchase_order_reference="PO1")
        facts = {
            "reference": "PO1",
            "currency": "USD",
            "amount": "100.01",
            "quantity": "2",
            "unit_price": "50.005",
        }
        self.proof("purchase_order", facts)
        self.proof("receipt", facts)
        result = self.retest().results.get(code="three_way")
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.evidence["checks"], [])

    def test_partial_receipt(self):
        self.invoice()
        self.reconcile(purchase_order_reference="PO1")
        facts = {
            "reference": "PO1",
            "currency": "USD",
            "amount": "100.01",
            "quantity": "2",
        }
        self.proof("purchase_order", facts)
        self.proof("receipt", {**facts, "quantity": "1"})
        result = self.retest().results.get(code="three_way")
        self.assertIn("partial_receipt", result.evidence["checks"])

    def test_missing_receipt_state(self):
        self.invoice()
        self.reconcile(purchase_order_reference="PO1")
        self.proof(
            "purchase_order",
            {"reference": "PO1", "currency": "USD", "amount": "100.01"},
        )
        result = self.retest().results.get(code="three_way")
        self.assertEqual(result.evidence["missing"], ["receipt"])

    def test_fabricated_support_value_rejected(self):
        self.ingest()
        with self.assertRaises(ValidationError):
            row = service.rows(self.audit).get()
            doc = self.source(raw=b"amount: 1.00")
            DocumentTextExtraction.objects.create(
                document=doc,
                extraction_method="plain_text",
                extracted_text="amount: 1.00",
                page_count=1,
                confidence_score=100,
            )
            service.support(
                self.admin,
                self.audit,
                row.uuid,
                revision=2,
                document_uuid=doc.uuid,
                role="support",
                evidence={
                    "proof": evidence(doc, "amount: 1.00", 0, 12),
                    "values": {"amount": "99.00"},
                },
            )

    def test_support_text_mutation_denies_evidence(self):
        self.ingest()
        link = self.proof("support", {"reference": "A1"})
        DocumentTextExtraction.objects.filter(document=link.document).update(
            extracted_text="Changed"
        )
        with self.assertRaises(PermissionDenied):
            service.authorize(self.admin, self.audit)

    def test_restricted_dataset_never_reaches_finance(self):
        doc = self.source()
        DocumentFieldPolicy.objects.create(
            document=doc, field_key="bank_account", can_view=False
        )
        with self.assertRaises(PermissionDenied):
            self.ingest(doc=doc)

    def test_field_revocation_blocks_findings(self):
        self.review()
        dataset = self.audit.datasets.get()
        DocumentFieldPolicy.objects.create(
            document=dataset.document, field_key="gross_amount", can_view=False
        )
        response = self.client.get(
            f"/api/workflows/finance-audits/{self.audit.uuid}/test-runs/"
        )
        self.assertEqual(response.status_code, 403)

    def test_export_field_restriction_blocks_all_raw_values(self):
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
        self.advance(run)
        dataset = self.audit.datasets.get()
        DocumentFieldPolicy.objects.create(
            document=dataset.document,
            field_key="gross_amount",
            can_view=True,
            can_export=False,
        )
        with self.assertRaises(PermissionDenied):
            service.export(self.admin, self.audit, self.audit.outputs.get())

    def test_primary_transaction_listing_prefetches(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.ingest([{**RECORD, "transaction_id": str(i)} for i in range(30)])
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                f"/api/workflows/finance-audits/{self.audit.uuid}/transactions/?page_size=30"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 30)
        self.assertLess(len(queries), 25)

    def test_large_import_is_bounded(self):
        with self.assertRaises(ValidationError):
            self.ingest([{**RECORD, "transaction_id": str(i)} for i in range(10001)])
        self.assertFalse(self.audit.datasets.exists())

    def test_exact_aggregate_does_not_round_large_decimals(self):
        dataset = self.ingest(
            [{**RECORD, "gross_amount": "9999999999999999999999.12345678"}] * 2
        )
        self.assertEqual(
            dataset.totals["computed"]["USD"]["gross_amount"],
            "19999999999999999999998.24691356",
        )

    def test_stratified_sampling_is_reproducible(self):
        self.ingest(
            [
                {
                    **RECORD,
                    "transaction_id": str(i),
                    "currency": "USD" if i % 2 else "EUR",
                }
                for i in range(12)
            ]
        )
        kwargs = {
            "revision": 2,
            "parameters": {"method": "stratified", "size": 4, "seed": "seed"},
        }
        a = service.sample(self.admin, self.audit, idempotency_key="a", **kwargs)
        b = service.sample(self.admin, self.audit, idempotency_key="b", **kwargs)
        self.assertEqual(a.selected, b.selected)
        self.assertEqual({x["currency"] for x in a.selected}, {"USD", "EUR"})

    def test_stale_sample_cannot_be_tested(self):
        self.review()
        sample = service.sample(
            self.admin,
            self.audit,
            revision=self.audit.revision,
            parameters={"method": "systematic", "size": 1},
            idempotency_key="sample",
        )
        row = service.rows(self.audit).get()
        service.correct(
            self.admin,
            self.audit,
            row.uuid,
            revision=self.audit.revision,
            values={**RECORD, "gross_amount": "1"},
            reason="Correction",
        )
        self.audit.refresh_from_db()
        with self.assertRaises(WorkflowConflict):
            service.execute(
                self.admin,
                self.audit,
                revision=self.audit.revision,
                sample_uuid=sample.uuid,
                idempotency_key="stale",
            )

    def test_duplicate_payment_decimal_scale_is_irrelevant(self):
        service.configure(
            self.admin,
            self.audit,
            revision=1,
            scope=SCOPE,
            policy={"tests": ["duplicates"]},
        )
        self.review(
            [
                {**RECORD, "payment_reference": "PAY", "gross_amount": "100"},
                {
                    **RECORD,
                    "transaction_id": "T2",
                    "payment_reference": "PAY",
                    "gross_amount": "100.00",
                },
            ]
        )
        results = self.audit.test_runs.get().results.all()
        self.assertEqual(
            {x.evidence["classification"] for x in results},
            {"exact_duplicate_candidate"},
        )

    def test_weak_signals_remain_probable(self):
        service.configure(
            self.admin,
            self.audit,
            revision=1,
            scope=SCOPE,
            policy={"tests": ["duplicates"]},
        )
        self.review(
            [
                {**RECORD, "invoice_reference": "INV"},
                {**RECORD, "transaction_id": "T2", "invoice_reference": "INV"},
            ]
        )
        results = self.audit.test_runs.get().results.all()
        self.assertEqual(
            {x.evidence["classification"] for x in results}, {"probable_duplicate"}
        )

    def test_duplicate_window_is_configurable(self):
        service.configure(
            self.admin,
            self.audit,
            revision=1,
            scope=SCOPE,
            policy={"tests": ["duplicates"], "duplicate_days": 1},
        )
        self.review(
            [
                {**RECORD, "invoice_reference": "INV"},
                {
                    **RECORD,
                    "transaction_id": "T2",
                    "invoice_reference": "INV",
                    "transaction_date": "2026-01-20",
                },
            ]
        )
        self.assertEqual(
            {x.status for x in self.audit.test_runs.get().results.all()}, {"passed"}
        )

    def test_cutoff_rule_is_configurable(self):
        service.configure(
            self.admin,
            self.audit,
            revision=1,
            scope=SCOPE,
            policy={"tests": ["cutoff"], "cutoff_days": 2},
        )
        self.review([{**RECORD, "transaction_date": "2026-01-30"}])
        result = self.audit.test_runs.get().results.get()
        self.assertEqual(result.status, "review_required")
        self.assertTrue(result.evidence["near_period_end"])

    def test_missing_support_creates_source_linked_finding(self):
        service.configure(
            self.admin,
            self.audit,
            revision=1,
            scope=SCOPE,
            policy={"tests": ["support"], "required_support_types": ["journal"]},
        )
        run = self.review()
        finding = run.findings.get()
        self.assertEqual(finding.category, "finance_support")
        self.assertEqual(
            finding.evidence[0]["transaction_uuid"],
            str(service.rows(self.audit).get().uuid),
        )

    def test_notifications_use_shared_infrastructure(self):
        with patch(
            "apps.workflows.services.finance_audit_service.notify_workflow"
        ) as notify:
            self.review()
        self.assertTrue(notify.called)

    def test_management_response_is_permission_checked(self):
        self.review([{**RECORD, "transaction_date": "2025-12-31"}])
        with self.assertRaises(PermissionDenied):
            service.respond(
                self.foreign,
                self.audit,
                self.audit.run.findings.get().uuid,
                revision=self.audit.revision,
                response="No",
                planned_action="None",
            )

    def test_source_proof_survives_in_result(self):
        self.ingest()
        link = self.proof("support", {"reference": "A1"})
        self.assertEqual(link.data["proof"]["preview"], "reference: A1")
        self.assertEqual(
            link.text_digest,
            service.link(
                self.admin,
                self.audit,
                link.transaction,
                link.document,
                "support",
                link.data,
            ).text_digest,
        )

    def test_test_library_version_is_pinned(self):
        self.review()
        self.assertEqual(self.audit.test_runs.get().definitions["library_version"], 1)

    def test_scope_dimension_rejects_wrong_business_unit(self):
        service.configure(
            self.admin,
            self.audit,
            revision=1,
            scope={**SCOPE, "business_units": ["West"]},
            policy={"tests": ["period"]},
        )
        with self.assertRaises(ValidationError):
            self.ingest([{**RECORD, "business_unit": "East"}])

    def test_evidence_request_links_response_and_shared_comment(self):
        self.review([{**RECORD, "transaction_date": "2025-12-31"}])
        finding = self.audit.run.findings.get()
        request = service.request_evidence(
            self.admin,
            self.audit,
            finding.uuid,
            revision=self.audit.revision,
            requested_item="Receipt",
            owner={"user": self.admin.pk},
            due_date="2026-02-01",
        )
        self.assertEqual(request.data["state"], "evidence_requested")
        self.assertTrue(
            self.audit.run.comments.filter(body__contains=str(request.uuid)).exists()
        )
        self.audit.refresh_from_db()
        response = service.respond(
            self.admin,
            self.audit,
            finding.uuid,
            revision=self.audit.revision,
            response="Receipt located",
            planned_action="Review evidence",
            request_uuid=request.uuid,
        )
        self.assertEqual(response.data["request_uuid"], str(request.uuid))
        request.refresh_from_db()
        self.assertEqual(request.data["state"], "evidence_requested")

    def test_request_is_not_sufficient_for_closure(self):
        self.review([{**RECORD, "transaction_date": "2025-12-31"}])
        finding = self.audit.run.findings.get()
        service.request_evidence(
            self.admin,
            self.audit,
            finding.uuid,
            revision=self.audit.revision,
            requested_item="Receipt",
            owner={"user": self.admin.pk},
        )
        self.audit.refresh_from_db()
        with self.assertRaises(ValidationError):
            service.decide(
                self.admin,
                self.audit,
                finding.uuid,
                revision=self.audit.revision,
                decision="closed",
                reason="Requested",
            )

    def test_generic_workflow_findings_revalidate_source_content(self):
        run = self.review([{**RECORD, "transaction_date": "2025-12-31"}])
        doc = self.audit.datasets.get().document
        with doc.file.open("wb") as stream:
            stream.write(b"Changed source")
        response = self.client.get(f"/api/workflows/runs/{run.uuid}/findings/")
        self.assertEqual(response.status_code, 403)

    def test_generic_finance_review_frozen_after_acceptance(self):
        run = self.review([{**RECORD, "transaction_date": "2025-12-31"}])
        finding = run.findings.get()
        service.decide(
            self.admin,
            self.audit,
            finding.uuid,
            revision=self.audit.revision,
            decision="accepted_risk",
            reason="Approved historical scope",
        )
        run.refresh_from_db()
        WorkflowEngineService.command(
            self.admin,
            run,
            action="accept",
            expected_revision=run.revision,
            idempotency_key="accept",
        )
        self.advance(run)
        with self.assertRaises(WorkflowConflict):
            WorkflowEngineService.review_finding(
                self.admin, run, finding.uuid, status="dismissed"
            )

    def test_unknown_transaction_uuid_cannot_access_corrections(self):
        self.ingest()
        response = self.client.get(
            f"/api/workflows/finance-audits/{self.audit.uuid}/transactions/{uuid.uuid4()}/corrections/"
        )
        self.assertEqual(response.status_code, 404)

    def test_finding_command_before_start_returns_404(self):
        response = self.client.post(
            f"/api/workflows/finance-audits/{self.audit.uuid}/findings/{uuid.uuid4()}/decide/",
            {"revision": 1, "decision": "closed", "reason": "No"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_support_without_offsets_rejected(self):
        row = self.ingest().transactions.get()
        doc = self.source(raw=b"Receipt")
        DocumentTextExtraction.objects.create(
            document=doc,
            extraction_method="plain_text",
            extracted_text="Receipt",
            page_count=1,
            confidence_score=100,
        )
        with self.assertRaises(ValidationError):
            service.support(
                self.admin,
                self.audit,
                row.uuid,
                revision=2,
                document_uuid=doc.uuid,
                role="receipt",
                evidence={
                    "proof": {"document_uuid": str(doc.uuid), "governance_revision": 1},
                    "values": {},
                },
            )

    def test_support_numeric_prefix_is_not_source_evidence(self):
        row = self.ingest().transactions.get()
        text = "amount: 100.01"
        doc = self.source(raw=text.encode())
        DocumentTextExtraction.objects.create(
            document=doc,
            extraction_method="plain_text",
            extracted_text=text,
            page_count=1,
            confidence_score=100,
        )
        with self.assertRaises(ValidationError):
            service.support(
                self.admin,
                self.audit,
                row.uuid,
                revision=2,
                document_uuid=doc.uuid,
                role="support",
                evidence={
                    "proof": evidence(doc, text, 0, len(text)),
                    "values": {"amount": "100"},
                },
            )

    def test_threshold_sample_filters_values(self):
        self.ingest(
            [
                {**RECORD, "transaction_id": str(i), "gross_amount": str(i)}
                for i in range(5)
            ]
        )
        sample = service.sample(
            self.admin,
            self.audit,
            revision=2,
            parameters={
                "method": "value_threshold",
                "size": 5,
                "thresholds": {"USD": "3"},
            },
            idempotency_key="threshold",
        )
        self.assertEqual(len(sample.selected), 2)

    def test_systematic_sample_is_bounded(self):
        self.ingest([{**RECORD, "transaction_id": str(i)} for i in range(20)])
        sample = service.sample(
            self.admin,
            self.audit,
            revision=2,
            parameters={"method": "systematic", "size": 3},
            idempotency_key="systematic",
        )
        self.assertEqual(len(sample.selected), 3)

    def test_no_dataset_start_rejected(self):
        with self.assertRaises(ValidationError):
            self.submit()

    def test_protected_library_has_no_write_endpoint(self):
        response = self.client.get("/api/workflows/finance-audits/test-library/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["protected"])
        self.client.force_authenticate(self.member)
        response = self.client.post(
            "/api/workflows/finance-audits/test-library/",
            {"sql": "unsafe"},
            format="json",
        )
        self.assertEqual(response.status_code, 405)

    def test_json_duplicate_fields_rejected(self):
        raw = (
            json.dumps(RECORD)
            .replace('"100.01"', '"100.01", "gross_amount": "0"')
            .encode()
        )
        with self.assertRaises(ValidationError):
            self.ingest(doc=self.source(raw=raw))

    def test_missing_gross_amount_cannot_pass_materiality(self):
        service.configure(
            self.admin,
            self.audit,
            revision=1,
            scope=SCOPE,
            policy={"tests": ["amount"], "materiality": {"USD": "100"}},
        )
        self.review(
            [
                {
                    **RECORD,
                    "gross_amount": None,
                    "lines": [{"debit": "1000", "credit": "1000"}],
                }
            ]
        )
        result = self.audit.test_runs.get().results.get()
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.evidence["amount"])

    def test_journal_line_controls_are_exact(self):
        dataset = self.ingest(
            [
                {
                    **RECORD,
                    "gross_amount": None,
                    "lines": [{"debit": "100.01"}, {"credit": "100.01"}],
                }
            ]
        )
        self.assertEqual(dataset.totals["computed"]["USD"]["debit"], "100.01")
        self.assertEqual(dataset.totals["computed"]["USD"]["credit"], "100.01")
        self.assertIsNone(dataset.totals["computed"]["USD"]["gross_amount"])

    def test_empty_threshold_sample_rejected(self):
        self.ingest()
        with self.assertRaises(ValidationError):
            service.sample(
                self.admin,
                self.audit,
                revision=2,
                parameters={
                    "method": "value_threshold",
                    "size": 1,
                    "thresholds": {"USD": "1000"},
                },
                idempotency_key="empty",
            )
        self.assertFalse(self.audit.samples.exists())

    def test_missing_dataset_file_denies_derivative_read(self):
        dataset = self.ingest()
        doc = dataset.document
        doc.file = ""
        doc.save(update_fields=["file"])
        response = self.client.get(
            f"/api/workflows/finance-audits/{self.audit.uuid}/transactions/"
        )
        self.assertEqual(response.status_code, 403)

    def test_malformed_finance_metadata_does_not_grant_workflow_content(self):
        from apps.workflows.permissions.workflow_permissions import (
            WorkflowPermissionService,
        )

        run = self.review()
        run.metadata["finance_audit_uuid"] = "invalid"
        self.assertFalse(WorkflowPermissionService.content_allowed(self.admin, run))

    def test_three_way_invoice_line_price_mismatch(self):
        invoice = self.invoice()
        invoice.normalized["line_items"] = [{"quantity": "2", "unit_price": "45"}]
        invoice.save(update_fields=["normalized"])
        self.reconcile(purchase_order_reference="PO1")
        facts = {
            "reference": "PO1",
            "currency": "USD",
            "amount": "100.01",
            "quantity": "2",
            "unit_price": "50.005",
        }
        self.proof("purchase_order", facts)
        self.proof("receipt", facts)
        result = self.retest().results.get(code="three_way")
        self.assertIn("po_invoice_unit_price_mismatch", result.evidence["checks"])

    def test_second_approved_output_has_new_version(self):
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
        self.advance(run)
        original = self.audit.outputs.get()
        self.ingest([{**RECORD, "transaction_id": "T2"}], key="next")
        run = self.advance(self.submit(key="second-run"))
        self.assertEqual(run.status, "review_required", run.error_type)
        WorkflowEngineService.command(
            self.admin,
            run,
            action="accept",
            expected_revision=run.revision,
            idempotency_key="second-accept",
        )
        self.advance(run)
        WorkflowEngineService.command(
            self.admin,
            run,
            action="approve",
            expected_revision=run.revision,
            idempotency_key="second-approve",
        )
        self.advance(run)
        self.assertEqual(run.status, "completed", run.error_type)
        self.assertEqual(self.audit.outputs.count(), 2)
        newest = self.audit.outputs.order_by("-number").first()
        self.assertEqual(newest.number, 2)
        self.assertNotEqual(
            newest.data["population_digest"], original.data["population_digest"]
        )

    def test_api_source_evidence_returns_pinned_proof(self):
        self.ingest()
        link = self.proof("support", {"reference": "E1"})
        response = self.client.get(
            f"/api/workflows/finance-audits/{self.audit.uuid}/transactions/{link.transaction.uuid}/evidence/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["results"][0]["data"]["proof"], link.data["proof"]
        )

    def test_server_date_account_and_decimal_filters(self):
        self.ingest(
            [
                RECORD,
                {
                    **RECORD,
                    "transaction_id": "T2",
                    "gross_amount": "200.02",
                    "transaction_date": "2026-01-20",
                },
            ]
        )
        response = self.client.get(
            f"/api/workflows/finance-audits/{self.audit.uuid}/transactions/?account=1000&date_from=2026-01-18&amount_min=200.01&amount_max=200.03"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["data"]["transaction_id"], "T2")

    def test_filters_follow_latest_correction(self):
        row = self.ingest().transactions.get()
        service.correct(
            self.admin,
            self.audit,
            row.uuid,
            revision=2,
            values={**RECORD, "currency": "EUR", "gross_amount": "250.01"},
            reason="Verified currency",
        )
        response = self.client.get(
            f"/api/workflows/finance-audits/{self.audit.uuid}/transactions/?currency=EUR&amount_min=250"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        response = self.client.get(
            f"/api/workflows/finance-audits/{self.audit.uuid}/transactions/?currency=USD"
        )
        self.assertEqual(response.data["count"], 0)

    def test_invalid_filter_decimal_is_rejected(self):
        self.ingest()
        response = self.client.get(
            f"/api/workflows/finance-audits/{self.audit.uuid}/transactions/?amount_min=NaN"
        )
        self.assertEqual(response.status_code, 400)

    def upload_and_import(self, kind, raw):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from apps.documents.models import Document

        uploaded = SimpleUploadedFile(
            "transactions." + kind,
            raw,
            content_type="application/octet-stream",
        )
        with patch(
            "apps.documents.services.document_processing_queue_service.DocumentProcessingQueueService.enqueue"
        ):
            response = self.client.post(
                "/api/documents/",
                {
                    "title": "Finance dataset",
                    "department": str(self.department.uuid),
                    "file": uploaded,
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 201, response.data)
        doc = Document.objects.get(uuid=response.data["uuid"])
        self.addCleanup(lambda: doc.file.delete(save=False))
        response = self.client.post(
            f"/api/workflows/finance-audits/{self.audit.uuid}/datasets/",
            {
                "revision": 1,
                "document_uuid": str(doc.uuid),
                "kind": kind,
                "mapping": {},
                "idempotency_key": "uploaded",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["row_count"], 1)
        self.assertEqual(response.data["checksum"], doc.checksum)
        return doc

    def test_normal_document_upload_then_json_audit_import(self):
        from apps.document_processing.services.text_extraction_service import (
            TextExtractionService,
        )

        raw = json.dumps([RECORD])
        doc = self.upload_and_import("json", raw.encode())
        extraction = TextExtractionService.extract_text(doc)
        self.assertEqual(extraction.extracted_text, raw)

    def test_normal_document_upload_then_csv_audit_import(self):
        raw = ",".join(RECORD) + "\n" + ",".join(RECORD.values()) + "\n"
        self.upload_and_import("csv", raw.encode())

    def test_closed_audit_supports_new_population_without_changing_output(self):
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
        self.advance(run)
        old = self.audit.outputs.get()
        before = json.dumps(old.data, sort_keys=True)
        run.refresh_from_db()
        metadata, revision = run.metadata.copy(), run.revision
        sources = set(run.documents.values_list("uuid", flat=True))
        self.ingest([{**RECORD, "transaction_id": "T2"}], key="next-population")
        old.refresh_from_db()
        self.assertEqual(json.dumps(old.data, sort_keys=True), before)
        run.refresh_from_db()
        self.assertEqual(run.metadata, metadata)
        self.assertEqual(run.revision, revision)
        self.assertEqual(set(run.documents.values_list("uuid", flat=True)), sources)
        response = self.client.get(f"/api/workflows/finance-audits/{self.audit.uuid}/")
        self.assertEqual(response.data["status"], "draft")
