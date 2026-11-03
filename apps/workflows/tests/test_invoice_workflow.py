import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.document_processing.models import DocumentTextExtraction
from apps.documents.models import Document, DocumentFieldPolicy
from apps.tenants.models import Company
from apps.workflows.models import (
    InvoiceAccountingHandoff,
    InvoiceBatch,
    InvoiceRecord,
    InvoiceVendor,
    WorkflowRun,
)
from apps.integrations.services.invoice_accounting import InvoiceAccountingService
from apps.workflows.services.invoice_definition_service import InvoiceDefinitionService
from apps.workflows.services.invoice_batch_service import InvoiceBatchService
from apps.workflows.services.invoice_extraction_service import (
    InvoiceExtractionService,
    evidence,
)
from apps.workflows.services.invoice_matching_service import InvoiceMatchingService
from apps.workflows.services.invoice_schema import (
    InvoicePolicy,
    normalize,
    number,
    normalized_date,
    validate_fields,
    validate_financials,
)
from apps.workflows.services.invoice_workflow_service import InvoiceWorkflowService
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
)
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService
from apps.workspaces.models import Department, DepartmentMembership


class InvoiceAccountingContractConnector:
    allowed_sensitivities = ("internal",)
    calls = []
    outcome = "posted"
    lookup_outcome = "absent"

    def post(self, *, company_id, payload, idempotency_key):
        self.calls.append(
            (company_id, idempotency_key, payload["invoice"]["invoice_number"])
        )
        if self.outcome == "timeout":
            raise TimeoutError("Ambiguous provider response")
        return {"status": self.outcome, "external_reference": "ERP-1001"}

    def lookup(self, *, company_id, idempotency_key):
        return {"status": self.lookup_outcome, "external_reference": "ERP-1001"}


class InvoiceSchemaTests(SimpleTestCase):
    def test_localized_decimals_and_leading_zero_identifier(self):
        for raw, expected in [
            ("$1,200.00", "1200.00"),
            ("1.200,00", "1200.00"),
            ("1 200,50", "1200.50"),
            ("(1200.50)", "-1200.50"),
            ("0.1", "0.1"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(number(raw), expected)
        data, errors = normalize(
            {"invoice_number": "000123", "vendor_identifier": "0007"},
            InvoicePolicy.validate({}),
        )
        self.assertFalse(errors)
        self.assertEqual(data["invoice_number"], "000123")
        self.assertEqual(data["vendor_identifier"], "0007")

    def test_ambiguous_and_impossible_numbers(self):
        for raw in [
            "1,200",
            "1.200",
            "NaN",
            "Infinity",
            "10%",
            "12,34,56",
            "1e2",
            "999999999999999999999",
        ]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                number(raw)
        self.assertEqual(number("1,200", "."), "1200")

    def test_dates_preserve_ambiguity(self):
        for raw in ["2026-10-07", "07 October 2026", "7 Oct 2026", "10/07/2026"]:
            self.assertEqual(normalized_date(raw, "MDY"), "2026-10-07")
        with self.assertRaises(ValueError):
            normalized_date("07/10/2026")
        with self.assertRaises(ValueError):
            normalized_date("2026-02-30")

    def test_currency_and_missing_values(self):
        data, errors = normalize({"currency": " usd "}, InvoicePolicy.validate({}))
        self.assertEqual(data["currency"], "USD")
        self.assertIsNone(data["tax_total"])
        self.assertIsNone(data["line_items"])
        self.assertFalse(errors)
        self.assertEqual(
            normalize({"currency": "$"}, InvoicePolicy.validate({}))[1][0]["code"],
            "invalid_currency",
        )

    def test_policy_rejects_bad_configuration(self):
        for options in [
            {"company": 3},
            {"require_po": "yes"},
            {"tolerance": "NaN"},
            {"confidence_threshold": "1.1"},
            {"schema_version": True},
            {"decimal_separator": []},
            {"date_order": {}},
            {"required_fields": ["unknown"]},
        ]:
            with self.subTest(options=options), self.assertRaises(ValidationError):
                InvoicePolicy.validate(options)

    def test_decimal_arithmetic_and_tolerance(self):
        policy = InvoicePolicy.validate({"tolerance": "0"})
        data, _ = normalize(
            {
                "currency": "USD",
                "subtotal": "0.1",
                "tax_total": "0.2",
                "grand_total": "0.3",
            },
            policy,
        )
        self.assertFalse(validate_financials(data, policy))
        data["grand_total"] = "0.31"
        self.assertEqual(
            validate_financials(data, policy)[0]["code"], "totals_mismatch"
        )
        self.assertFalse(
            validate_financials(data, InvoicePolicy.validate({"tolerance": "0.01"}))
        )

    def test_line_tax_rate_is_not_tax_amount(self):
        raw = {
            "subtotal": "100",
            "grand_total": "110",
            "tax_total": "10",
            "line_items": [
                {
                    "quantity": "2",
                    "unit_price": "50",
                    "line_subtotal": "100",
                    "tax_rate": "10%",
                    "tax_amount": "10",
                    "line_total": "110",
                }
            ],
        }
        policy = InvoicePolicy.validate({})
        data, errors = normalize(raw, policy)
        self.assertFalse(errors)
        self.assertFalse(validate_financials(data, policy))
        data["line_items"][0]["tax_amount"] = "20"
        self.assertIn(
            "tax_validation_failed",
            [x["code"] for x in validate_financials(data, policy)],
        )

    def test_line_discounts_are_not_subtracted_twice(self):
        policy = InvoicePolicy.validate({"tolerance": "0"})
        cases = (
            # All discount is allocated to the line item.
            ("5", "105", "105", "5"),
            # Additional header-level discount after the line discount.
            ("7", "103", "105", "5"),
            # Invoice-only discount: line total is undiscounted.
            ("5", "105", "110", None),
        )
        for header_discount, grand_total, line_total, line_discount in cases:
            with self.subTest(
                header_discount=header_discount, line_discount=line_discount
            ):
                row = {
                    "quantity": "1",
                    "unit_price": "100",
                    "line_subtotal": "100",
                    "tax_amount": "10",
                    "line_total": line_total,
                }
                if line_discount is not None:
                    row["discount"] = line_discount
                data, errors = normalize(
                    {
                        "currency": "USD",
                        "subtotal": "100",
                        "tax_total": "10",
                        "discount_total": header_discount,
                        "grand_total": grand_total,
                        "line_items": [row],
                    },
                    policy,
                )
                self.assertFalse(errors)
                self.assertEqual(validate_financials(data, policy), [])

    def test_line_discounts_larger_than_header_are_flagged(self):
        policy = InvoicePolicy.validate({"tolerance": "0"})
        data, errors = normalize(
            {
                "currency": "USD",
                "subtotal": "100",
                "tax_total": "10",
                "discount_total": "3",
                "grand_total": "107",
                "line_items": [
                    {
                        "quantity": "1",
                        "unit_price": "100",
                        "line_subtotal": "100",
                        "discount": "5",
                        "tax_amount": "10",
                        "line_total": "105",
                    }
                ],
            },
            policy,
        )
        self.assertFalse(errors)
        self.assertIn(
            "discount_allocation_mismatch",
            [finding["code"] for finding in validate_financials(data, policy)],
        )

    def test_line_discrepancies_and_structural_limits(self):
        policy = InvoicePolicy.validate({})
        data, _ = normalize(
            {
                "subtotal": "10",
                "grand_total": "10",
                "line_items": [
                    {
                        "quantity": "3",
                        "unit_price": "2",
                        "line_subtotal": "7",
                        "line_total": "9",
                    }
                ],
            },
            policy,
        )
        self.assertEqual(
            set(x["code"] for x in validate_financials(data, policy)),
            {
                "line_subtotal_mismatch",
                "line_total_mismatch",
                "line_items_subtotal_mismatch",
                "line_items_total_mismatch",
            },
        )
        for rows in [[{"unknown": "3"}], [None], [{}] * 201]:
            self.assertTrue(normalize({"line_items": rows}, policy)[1])

    def test_due_dates_required_fields_and_precision(self):
        policy = InvoicePolicy.validate({})
        data, _ = normalize(
            {
                "invoice_date": "2026-10-07",
                "due_date": "2026-10-01",
                "currency": "JPY",
                "subtotal": "10.1",
            },
            policy,
        )
        codes = [x["code"] for x in validate_fields(data, policy)]
        self.assertIn("invalid_date", codes)
        self.assertIn("missing_required_field", codes)
        self.assertIn("invalid_currency_precision", codes)

    def test_amount_due_difference_is_warning(self):
        data, _ = normalize(
            {"grand_total": "100", "amount_due": "50"}, InvoicePolicy.validate({})
        )
        self.assertFalse(
            validate_financials(data, InvoicePolicy.validate({}))[0]["blocking"]
        )

    def test_money_currency_conflicts_and_numeric_units_are_rejected(self):
        policy = InvoicePolicy.validate({})
        for amount in ["EUR 100.00", "100.00 EUR", "\u20ac100.00", "ABC 100.00"]:
            data, errors = normalize({"currency": "USD", "grand_total": amount}, policy)
            self.assertIsNone(data["grand_total"])
            self.assertEqual(errors[0]["code"], "invalid_currency_amount")
        self.assertEqual(number("($1,200.00)"), "-1200.00")
        self.assertEqual(number("usd 100.00", currency="USD"), "100.00")
        data, errors = normalize(
            {"line_items": [{"quantity": "$2", "tax_rate": "USD 10%"}]}, policy
        )
        self.assertEqual(len(errors), 2)

    def test_untrusted_ai_schema_and_source_proof(self):
        chunk = {"chunk_uuid": str(uuid.uuid4()), "content": "Invoice number: 0007"}
        valid = {
            "fields": {
                "invoice_number": {
                    "raw_value": "0007",
                    "confidence": 0.9,
                    "evidence": [chunk["chunk_uuid"]],
                }
            }
        }
        self.assertEqual(
            InvoiceExtractionService.validate_ai_output(json.dumps(valid), [chunk]),
            valid,
        )
        for answer in [
            "not json",
            "[]",
            '{"findings": []}',
            '{"fields":{"unknown":{}}}',
        ]:
            with self.subTest(answer=answer), self.assertRaises(ValidationError):
                InvoiceExtractionService.validate_ai_output(answer, [chunk])
        for raw, confidence, refs in [
            ("fabricated", 0.9, [chunk["chunk_uuid"]]),
            ("0007", 1.2, [chunk["chunk_uuid"]]),
            ("0007", 0.9, [str(uuid.uuid4())]),
        ]:
            valid["fields"]["invoice_number"].update(
                raw_value=raw, confidence=confidence, evidence=refs
            )
            with self.assertRaises(ValidationError):
                InvoiceExtractionService.validate_ai_output(json.dumps(valid), [chunk])

    def test_ai_line_items_are_grounded_per_row_not_across_unrelated_chunks(self):
        first = {
            "chunk_uuid": str(uuid.uuid4()),
            "content": "Description: Service A | Qty: 2 | Price: 50",
        }
        second = {
            "chunk_uuid": str(uuid.uuid4()),
            "content": "Description: Service B | Qty: 3 | Price: 80",
        }
        chunks = [first, second]
        refs = [c["chunk_uuid"] for c in chunks]
        good = {
            "fields": {
                "line_items": {
                    "raw_value": [
                        {
                            "description": "Service A",
                            "quantity": "2",
                            "unit_price": "50",
                        },
                        {
                            "description": "Service B",
                            "quantity": "3",
                            "unit_price": "80",
                        },
                    ],
                    "confidence": 0.9,
                    "evidence": refs,
                }
            }
        }
        self.assertEqual(
            InvoiceExtractionService.validate_ai_output(json.dumps(good), chunks),
            good,
        )
        # All values exist in the combined evidence, but no single chunk
        # supports the claimed Service A / 80 combination.
        mixed = json.loads(json.dumps(good))
        mixed["fields"]["line_items"]["raw_value"] = [
            {"description": "Service A", "quantity": "2", "unit_price": "80"}
        ]
        with self.assertRaises(ValidationError):
            InvoiceExtractionService.validate_ai_output(json.dumps(mixed), chunks)

    def test_ai_line_items_reject_empty_rows(self):
        chunk = {"chunk_uuid": str(uuid.uuid4()), "content": "Description: Service"}
        payload = {
            "fields": {
                "line_items": {
                    "raw_value": [],
                    "confidence": 0.8,
                    "evidence": [chunk["chunk_uuid"]],
                }
            }
        }
        for rows in ([], [{}], [{"description": None}], [{"description": ""}]):
            with self.subTest(rows=rows), self.assertRaises(ValidationError):
                payload["fields"]["line_items"]["raw_value"] = rows
                InvoiceExtractionService.validate_ai_output(
                    json.dumps(payload), [chunk]
                )


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
class InvoiceWorkflowTests(APITestCase):
    TEXT = "Tax invoice\nInvoice number: 000123\nInvoice date: 2026-10-07\nDue date: 2026-11-07\nVendor name: Acme\nVendor identifier: 0007\nBill to name: Buyer\nCurrency: usd\nSubtotal: 100.00\nTax total: 10.00\nGrand total: 110.00\nAmount due: 110.00\n"

    @classmethod
    def setUpTestData(cls):
        from apps.workflows.tests.system_reference_data import ensure_invoice_schema
        ensure_invoice_schema()
        cls.company = Company.objects.create(name="Invoices", slug="invoices")
        cls.other = Company.objects.create(name="Other invoices", slug="other-invoices")
        cls.admin = User.objects.create_user(
            username="invoice-admin", company=cls.company, role="company_admin"
        )
        cls.reviewer = User.objects.create_user(
            username="invoice-reviewer", company=cls.company, role="reviewer"
        )
        cls.member = User.objects.create_user(
            username="invoice-member", company=cls.company
        )
        cls.foreign = User.objects.create_user(
            username="invoice-foreign", company=cls.other, role="company_admin"
        )
        cls.department = Department.objects.create(company=cls.company, name="Finance")
        DepartmentMembership.objects.create(
            company=cls.company,
            department=cls.department,
            user=cls.reviewer,
            role="reviewer",
        )
        DepartmentMembership.objects.create(
            company=cls.company,
            department=cls.department,
            user=cls.member,
            role="member",
        )

    def setUp(self):
        self.enqueue_patch = patch.object(WorkflowEngineService, "enqueue")
        self.enqueue_patch.start()
        self.addCleanup(patch.stopall)
        self.client.force_authenticate(self.admin)
        self.document = self.make_document()

    def make_document(self, text=None, company=None, checksum=""):
        document = Document.objects.create(
            company=company or self.company,
            department=self.department if not company else None,
            uploaded_by=self.admin if not company else self.foreign,
            title="Invoice",
            document_type="invoice",
            original_filename="invoice.txt",
            status="ready",
            is_ai_ready=True,
            is_indexed=True,
            checksum=checksum,
        )
        DocumentTextExtraction.objects.create(
            document=document,
            extraction_method="plain_text",
            extracted_text=self.TEXT if text is None else text,
            page_count=1,
            confidence_score=100,
        )
        return document

    def version(self, **policy):
        _, version = InvoiceDefinitionService.create(
            self.admin,
            name="Invoice",
            code="invoice-" + uuid.uuid4().hex[:12],
            reviewer={"user": self.reviewer.pk},
            department=self.department.pk,
            policy={"require_approval": False, **policy},
        )
        return version

    def start(self, version=None, documents=None, key=None):
        run, _ = WorkflowEngineService.start(
            self.admin,
            version_uuid=(version or self.version()).uuid,
            document_uuids=[str(d.uuid) for d in (documents or [self.document])],
            idempotency_key=key or uuid.uuid4().hex,
        )
        return run

    def advance(self, run):
        for _ in range(30):
            run.refresh_from_db()
            if run.status != "queued":
                return run
            WorkflowExecutorService.execute(run.company_id, str(run.uuid), run.revision)
        self.fail("Invoice workflow did not reach a bounded wait or terminal state")

    def test_batch_creates_independent_runs_and_is_idempotent(self):
        version = self.version()
        second = self.make_document()
        payload = {
            "name": "October AP",
            "version": str(version.uuid),
            "documents": [str(self.document.uuid), str(second.uuid)],
            "idempotency_key": "october-ap-1",
        }
        response = self.client.post(
            "/api/workflows/invoice-batches/", payload, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["progress"]["total"], 2)
        self.assertTrue(
            all(item["status"] == "queued" for item in response.data["items"])
        )
        self.assertFalse(
            InvoiceBatchService.start_waiting(self.company.pk, response.data["uuid"])
        )
        detail = self.client.get(
            f'/api/workflows/invoice-batches/{response.data["uuid"]}/'
        )
        runs = [item["run_uuid"] for item in detail.data["items"]]
        self.assertEqual(len(set(runs)), 2)
        self.assertTrue(all(runs))
        repeated = self.client.post(
            "/api/workflows/invoice-batches/", payload, format="json"
        )
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(repeated.data["uuid"], response.data["uuid"])
        self.assertEqual(InvoiceBatch.objects.count(), 1)
        self.assertEqual(
            self.client.get("/api/workflows/invoice-batches/").data["count"], 1
        )

    def test_batch_rejects_inaccessible_document_and_key_conflict(self):
        version = self.version()
        payload = {
            "name": "Restricted AP",
            "version": str(version.uuid),
            "documents": [
                str(self.document.uuid),
                str(self.make_document(company=self.other).uuid),
            ],
            "idempotency_key": "ap-restricted",
        }
        self.assertEqual(
            self.client.post(
                "/api/workflows/invoice-batches/", payload, format="json"
            ).status_code,
            403,
        )
        self.assertEqual(InvoiceBatch.objects.count(), 0)
        payload["documents"] = [str(self.document.uuid)]
        self.assertEqual(
            self.client.post(
                "/api/workflows/invoice-batches/", payload, format="json"
            ).status_code,
            201,
        )
        payload["name"] = "Different payload"
        self.assertEqual(
            self.client.post(
                "/api/workflows/invoice-batches/", payload, format="json"
            ).status_code,
            409,
        )

    def test_batch_read_fails_closed_after_document_access_revoked(self):
        version = self.version()
        response = self.client.post(
            "/api/workflows/invoice-batches/",
            {
                "name": "Access check",
                "version": str(version.uuid),
                "documents": [str(self.document.uuid)],
                "idempotency_key": "ap-access",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        batch_url = f'/api/workflows/invoice-batches/{response.data["uuid"]}/'
        self.document.retention_start = timezone.now() - timedelta(days=3)
        self.document.retention_expires_at = timezone.now() - timedelta(days=1)
        self.document.save(
            update_fields=["retention_start", "retention_expires_at", "updated_at"]
        )
        self.assertEqual(self.client.get(batch_url).status_code, 404)

    def test_batch_partial_start_failure_can_retry_only_that_item(self):
        version = self.version()
        second = self.make_document()
        original_start = WorkflowEngineService.start

        def start_or_fail(user, **kwargs):
            if kwargs["document_uuids"] == [str(second.uuid)]:
                raise ValidationError("Invoice source needs review.")
            return original_start(user, **kwargs)

        response = self.client.post(
            "/api/workflows/invoice-batches/",
            {
                "name": "Mixed AP",
                "version": str(version.uuid),
                "documents": [str(self.document.uuid), str(second.uuid)],
                "idempotency_key": "ap-mixed",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        with patch.object(WorkflowEngineService, "start", side_effect=start_or_fail):
            self.assertFalse(
                InvoiceBatchService.start_waiting(
                    self.company.pk, response.data["uuid"]
                )
            )
        detail = self.client.get(
            f'/api/workflows/invoice-batches/{response.data["uuid"]}/'
        )
        self.assertEqual(detail.data["progress"]["failed"], 1)
        self.assertTrue(detail.data["items"][0]["run_uuid"])
        failed = detail.data["items"][1]
        self.assertIsNone(failed["run_uuid"])
        retry_url = (
            f'/api/workflows/invoice-batches/{response.data["uuid"]}/retry-item/'
        )
        retried = self.client.post(
            retry_url, {"item_uuid": failed["uuid"]}, format="json"
        )
        self.assertEqual(retried.status_code, 200, retried.data)
        self.assertEqual(retried.data["progress"]["failed"], 0)
        self.assertTrue(retried.data["items"][1]["run_uuid"])
        self.assertEqual(
            self.client.post(
                retry_url, {"item_uuid": failed["uuid"]}, format="json"
            ).status_code,
            409,
        )

    def test_batch_waits_for_document_processing_and_redelivery_is_idempotent(self):
        version = self.version()
        self.document.status = "processing"
        self.document.save(update_fields=["status", "updated_at"])
        response = self.client.post(
            "/api/workflows/invoice-batches/",
            {
                "name": "Queued AP",
                "version": str(version.uuid),
                "documents": [str(self.document.uuid)],
                "idempotency_key": "ap-queued",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["items"][0]["status"], "queued")
        self.assertIsNone(response.data["items"][0]["run_uuid"])
        self.document.status = "ready"
        self.document.save(update_fields=["status", "updated_at"])
        self.assertFalse(
            InvoiceBatchService.start_waiting(self.company.pk, response.data["uuid"])
        )
        self.assertFalse(
            InvoiceBatchService.start_waiting(self.company.pk, response.data["uuid"])
        )
        detail = self.client.get(
            f'/api/workflows/invoice-batches/{response.data["uuid"]}/'
        )
        self.assertTrue(detail.data["items"][0]["run_uuid"])
        self.assertEqual(WorkflowRun.objects.filter(company=self.company).count(), 1)

    def test_batch_does_not_start_after_initiator_revocation(self):
        version = self.version()
        self.document.status = "processing"
        self.document.save(update_fields=["status", "updated_at"])
        response = self.client.post(
            "/api/workflows/invoice-batches/",
            {
                "name": "Revoked AP",
                "version": str(version.uuid),
                "documents": [str(self.document.uuid)],
                "idempotency_key": "ap-revoked",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.document.status = "ready"
        self.document.save(update_fields=["status", "updated_at"])
        self.admin.is_active = False
        self.admin.save(update_fields=["is_active"])
        self.assertFalse(
            InvoiceBatchService.start_waiting(self.company.pk, response.data["uuid"])
        )
        self.assertEqual(WorkflowRun.objects.filter(company=self.company).count(), 0)
        self.assertEqual(
            InvoiceBatch.objects.get(uuid=response.data["uuid"])
            .items.first()
            .error_code,
            "actor_unavailable",
        )

    def test_accounting_is_not_connected_without_a_provider(self):
        invoice = self.advance(self.start()).invoice
        url = f"/api/workflows/invoices/{invoice.uuid}/"
        self.assertEqual(
            self.client.get(url + "accounting-status/").data["status"], "not_connected"
        )
        response = self.client.post(
            url + "post-accounting/", {"idempotency_key": "posting-1"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(InvoiceAccountingHandoff.objects.exists())

    @override_settings(
        DOCUX_INVOICE_ACCOUNTING_CONNECTOR="apps.workflows.tests.test_invoice_workflow.InvoiceAccountingContractConnector"
    )
    def test_accounting_posts_only_after_explicit_authorized_request_and_is_idempotent(
        self,
    ):
        InvoiceAccountingContractConnector.calls = []
        InvoiceAccountingContractConnector.outcome = "posted"
        invoice = self.advance(self.start()).invoice
        url = f"/api/workflows/invoices/{invoice.uuid}/"
        self.client.force_authenticate(self.member)
        self.assertEqual(
            self.client.post(
                url + "post-accounting/",
                {"idempotency_key": "posting-1"},
                format="json",
            ).status_code,
            403,
        )
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            url + "post-accounting/", {"idempotency_key": "posting-1"}, format="json"
        )
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["status"], "pending")
        self.assertEqual(InvoiceAccountingContractConnector.calls, [])
        self.assertTrue(
            InvoiceAccountingService.process(
                self.company.pk, response.data["handoff_uuid"]
            )
        )
        self.assertFalse(
            InvoiceAccountingService.process(
                self.company.pk, response.data["handoff_uuid"]
            )
        )
        self.assertEqual(len(InvoiceAccountingContractConnector.calls), 1)
        self.assertEqual(
            self.client.get(url + "accounting-status/").data["status"], "posted"
        )
        self.assertEqual(
            self.client.post(
                url + "post-accounting/",
                {"idempotency_key": "posting-1"},
                format="json",
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                url + "post-accounting/",
                {"idempotency_key": "posting-2"},
                format="json",
            ).status_code,
            409,
        )

    @override_settings(
        DOCUX_INVOICE_ACCOUNTING_CONNECTOR="apps.workflows.tests.test_invoice_workflow.InvoiceAccountingContractConnector"
    )
    def test_ambiguous_posting_requires_provider_reconciliation(self):
        InvoiceAccountingContractConnector.calls = []
        InvoiceAccountingContractConnector.outcome = "timeout"
        InvoiceAccountingContractConnector.lookup_outcome = "absent"
        invoice = self.advance(self.start()).invoice
        url = f"/api/workflows/invoices/{invoice.uuid}/"
        response = self.client.post(
            url + "post-accounting/",
            {"idempotency_key": "posting-ambiguous"},
            format="json",
        )
        self.assertEqual(response.status_code, 202, response.data)
        self.assertFalse(
            InvoiceAccountingService.process(
                self.company.pk, response.data["handoff_uuid"]
            )
        )
        self.assertEqual(
            self.client.get(url + "accounting-status/").data["status"], "uncertain"
        )
        self.assertEqual(len(InvoiceAccountingContractConnector.calls), 1)
        self.assertEqual(
            self.client.post(url + "reconcile-accounting/", {}, format="json").data[
                "status"
            ],
            "pending",
        )
        InvoiceAccountingContractConnector.outcome = "posted"
        self.assertTrue(
            InvoiceAccountingService.process(
                self.company.pk, response.data["handoff_uuid"]
            )
        )
        self.assertEqual(len(InvoiceAccountingContractConnector.calls), 2)
        self.assertEqual(
            InvoiceAccountingContractConnector.calls[0][1],
            InvoiceAccountingContractConnector.calls[1][1],
        )

    def accept(self, run):
        return WorkflowEngineService.command(
            self.reviewer,
            run,
            action="accept",
            expected_revision=run.revision,
            idempotency_key=uuid.uuid4().hex,
        )

    def test_valid_invoice_completes_with_stable_canonical_output(self):
        run = self.advance(self.start())
        self.assertEqual(run.status, "completed")
        invoice = run.invoice
        self.assertEqual(invoice.normalized["invoice_number"], "000123")
        self.assertEqual(invoice.normalized["invoice_date"], "2026-10-07")
        self.assertEqual(invoice.normalized["currency"], "USD")
        self.assertEqual(Decimal(invoice.normalized["grand_total"]), Decimal("110"))
        self.assertEqual(invoice.effective_confidence, Decimal("0.92"))
        self.assertEqual(invoice.outputs.count(), 1)
        output = invoice.outputs.get()
        self.assertEqual(
            InvoiceWorkflowService.finalize(
                invoice, InvoicePolicy.validate({"require_approval": False})
            ),
            output.data,
        )
        self.assertEqual(invoice.outputs.count(), 1)
        output.digest = "changed"
        with self.assertRaises(DjangoValidationError):
            output.save()

    def test_provenance_contains_real_offsets_and_page(self):
        run = self.advance(self.start())
        proof = InvoiceWorkflowService.provenance(run.invoice)["invoice_number"]
        self.assertEqual(proof["raw_value"], "000123")
        self.assertEqual(proof["evidence"][0]["document_uuid"], str(self.document.uuid))
        self.assertEqual(proof["evidence"][0]["page"], 1)
        self.assertEqual(proof["evidence"][0]["preview"], "000123")
        self.document.text_extraction.page_count = 0
        self.assertIsNone(evidence(self.document, "text", 0, 4, "test")["page"])

    def test_missing_field_requires_correction_and_preserves_history(self):
        self.document.text_extraction.extracted_text = self.TEXT.replace(
            "Grand total: 110.00\n", ""
        )
        self.document.text_extraction.save()
        run = self.advance(self.start())
        self.assertEqual(run.status, "review_required")
        with self.assertRaises(ValidationError):
            self.accept(run)
        field = run.invoice.extraction_run.result_fields.get(field_key="grand_total")
        InvoiceWorkflowService.correct(
            self.reviewer, field, "110.00", run.revision, "Verified against source"
        )
        run.refresh_from_db()
        field.refresh_from_db()
        self.assertEqual(field.extracted_value, "")
        self.assertEqual(field.corrections.get().previous_value, "")
        self.assertEqual(field.corrections.get().actor, self.reviewer)
        self.assertTrue(
            AuditLog.objects.filter(metadata__event="invoice_field_corrected").exists()
        )
        self.advance(self.accept(run))
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")
        self.assertEqual(
            run.invoice.outputs.get().data["invoice"]["grand_total"], "110.00"
        )

    def test_correction_authorization_revision_and_reason(self):
        run = self.advance(self.start(self.version(force_review=True)))
        field = run.invoice.extraction_run.result_fields.get(field_key="subtotal")
        for user in [self.member, self.foreign]:
            with self.assertRaises(PermissionDenied):
                InvoiceWorkflowService.correct(
                    user, field, "100", run.revision, "reason"
                )
        with self.assertRaises(WorkflowConflict):
            InvoiceWorkflowService.correct(
                self.reviewer, field, "100", run.revision - 1, "reason"
            )
        with self.assertRaises(ValidationError):
            InvoiceWorkflowService.correct(
                self.reviewer, field, "100", run.revision, ""
            )

    def test_multiple_corrections_keep_original_and_append_history(self):
        run = self.advance(self.start(self.version(force_review=True)))
        field = run.invoice.extraction_run.result_fields.get(field_key="vendor_name")
        original_metadata = field.source_metadata
        for value in ["Acme Ltd", "Acme Limited"]:
            InvoiceWorkflowService.correct(
                self.reviewer, field, value, run.revision, "Business name verified"
            )
            run.refresh_from_db()
        field.refresh_from_db()
        self.assertEqual(field.extracted_value, "Acme")
        self.assertEqual(field.source_metadata, original_metadata)
        self.assertEqual(
            list(field.corrections.values_list("previous_value", flat=True)),
            ["Acme", "Acme Ltd"],
        )

    def test_invalid_totals_create_structured_findings(self):
        self.document.text_extraction.extracted_text = self.TEXT.replace(
            "Grand total: 110.00", "Grand total: 120.00"
        )
        self.document.text_extraction.save()
        run = self.advance(self.start())
        self.assertEqual(run.status, "review_required")
        finding = run.findings.get(category="totals_mismatch")
        self.assertTrue(finding.data["blocking"])
        with self.assertRaises(ValidationError):
            self.accept(run)
        WorkflowEngineService.review_finding(
            self.reviewer, run, finding.uuid, status="accepted"
        )
        self.advance(self.accept(run))
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")

    def test_low_ocr_confidence_requires_review(self):
        extraction = self.document.text_extraction
        extraction.extraction_method = "ocr"
        extraction.confidence_score = 30
        extraction.save()
        run = self.advance(self.start())
        self.assertEqual(run.status, "review_required")
        self.assertTrue(run.findings.filter(category="low_confidence").exists())

    def test_celery_redelivery_and_start_idempotency(self):
        version = self.version()
        run = self.start(version, key="same-start")
        self.assertEqual(self.start(version, key="same-start").pk, run.pk)
        revision = run.revision
        WorkflowExecutorService.execute(run.company_id, str(run.uuid), revision)
        WorkflowExecutorService.execute(run.company_id, str(run.uuid), revision)
        self.advance(run)
        self.assertEqual(InvoiceRecord.objects.count(), 1)
        self.assertEqual(run.invoice.extraction_run.result_fields.count(), 23)

    def test_exact_and_probable_duplicates_are_candidates(self):
        first = self.advance(self.start()).invoice
        second = self.make_document()
        run = self.advance(self.start(documents=[second]))
        self.assertEqual(run.status, "review_required")
        self.assertEqual(run.invoice.duplicates[0]["match_strength"], "exact")
        self.assertEqual(run.invoice.duplicates[0]["status"], "candidate")
        run.invoice.normalized["grand_total"] = "111.00"
        run.invoice.fingerprint = "different"
        run.invoice.save()
        candidates = InvoiceMatchingService.duplicates(run.invoice, self.admin)
        self.assertEqual(candidates[0]["match_strength"], "probable")
        self.assertEqual(candidates[0]["invoice_uuid"], str(first.uuid))

    def test_vendor_exact_ambiguous_and_unmatched(self):
        invoice = self.advance(self.start()).invoice
        self.assertEqual(
            InvoiceMatchingService.vendor(invoice)["status"], "not_matched"
        )
        vendor = InvoiceVendor.objects.create(
            company=self.company, name="Acme", identifier="0007"
        )
        self.assertEqual(
            InvoiceMatchingService.vendor(invoice)["vendor_uuid"], str(vendor.uuid)
        )
        InvoiceVendor.objects.create(
            company=self.company, name="Acme", identifier="0007"
        )
        self.assertEqual(InvoiceMatchingService.vendor(invoice)["status"], "ambiguous")

    def test_po_adapter_matching_and_mismatch(self):
        invoice = self.advance(self.start()).invoice
        invoice.normalized["purchase_order_number"] = "PO-1"
        policy = InvoicePolicy.validate({"po_adapter": "test"})
        self.assertEqual(
            InvoiceMatchingService.purchase_order(invoice, self.admin, policy)[
                "status"
            ],
            "integration_unavailable",
        )

        class Adapter:
            @staticmethod
            def match(**kwargs):
                return {
                    "company_id": kwargs["company"].pk,
                    "status": "matched",
                    "reference": "PO-1",
                    "currency": "USD",
                    "amount": "110.00",
                }

        with patch.dict(
            InvoiceMatchingService.purchase_order_adapters, {"test": Adapter}
        ):
            self.assertEqual(
                InvoiceMatchingService.purchase_order(invoice, self.admin, policy)[
                    "checks"
                ],
                [],
            )
            invoice.normalized["grand_total"] = "120.00"
            self.assertEqual(
                InvoiceMatchingService.purchase_order(invoice, self.admin, policy)[
                    "checks"
                ],
                ["po_amount_mismatch"],
            )

    def test_invoice_rejects_multi_document_without_changing_generic_engine(self):
        with self.assertRaises(ValidationError):
            self.start(documents=[self.document, self.make_document()])

    def test_unrelated_document_is_rejected(self):
        document = self.make_document(
            "Agreement contract termination jurisdiction party"
        )
        run = self.advance(self.start(documents=[document]))
        self.assertEqual(run.status, "failed")
        self.assertIsNone(run.invoice.extraction_run_id)
        self.assertFalse(run.invoice.outputs.exists())

    def test_current_permissions_protect_api_export_and_tenant(self):
        run = self.advance(self.start())
        url = f"/api/workflows/invoices/{run.invoice.uuid}/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url + "export/").status_code, 200)
        self.client.force_authenticate(self.foreign)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_authenticate(self.admin)
        self.document.retention_expires_at = timezone.now() - timedelta(days=1)
        self.document.retention_start = timezone.now() - timedelta(days=3)
        self.document.save()
        self.assertIn(self.client.get(url).status_code, [403, 404])

    def test_invoice_read_actions_and_no_state_patch(self):
        run = self.advance(self.start())
        url = f"/api/workflows/invoices/{run.invoice.uuid}/"
        for suffix in ["", "fields/", "outputs/", "export/"]:
            response = self.client.get(url + suffix)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(
            self.client.patch(url, {"status": "completed"}).status_code, 405
        )

    def test_derivatives_remain_linked_to_source(self):
        invoice = self.advance(self.start()).invoice
        self.assertEqual(invoice.document_id, self.document.pk)
        self.assertEqual(invoice.extraction_run.document_id, self.document.pk)
        self.assertEqual(
            invoice.outputs.get().data["source"]["document_uuid"],
            str(self.document.uuid),
        )

    def test_approval_uses_existing_request_and_double_submission_is_safe(self):
        from apps.approvals.services import ApprovalWorkflowService

        run = self.advance(self.start(self.version(require_approval=True)))
        self.assertEqual(run.status, "approval_required")
        approval = run.step_runs.get(step=run.current_step).approval
        self.assertEqual(approval.review_type, "workflow_review")
        self.assertEqual(approval.submitted_payload, {"workflow_uuid": str(run.uuid)})
        ApprovalWorkflowService.approve(self.reviewer, approval)
        with self.assertRaises(ValidationError):
            ApprovalWorkflowService.approve(self.reviewer, approval)
        self.advance(run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(
            run.invoice.outputs.get().data["approval_uuid"], str(approval.uuid)
        )

    def test_review_double_submission_is_idempotent(self):
        run = self.advance(self.start(self.version(force_review=True)))
        revision = run.revision
        for _ in range(2):
            WorkflowEngineService.command(
                self.reviewer,
                run,
                action="accept",
                expected_revision=revision,
                idempotency_key="one-review",
            )
        self.advance(run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.commands.count(), 1)

    def test_field_restriction_prevents_extraction_and_current_reads(self):
        version = self.version(use_ai=True)
        policy = DocumentFieldPolicy.objects.create(
            document=self.document,
            field_key="vendor_tax_id",
            user=self.admin,
            can_view=True,
            can_use_ai=False,
        )
        with self.assertRaises(PermissionDenied):
            self.start(version)
        policy.delete()
        run = self.advance(self.start())
        DocumentFieldPolicy.objects.create(
            document=self.document,
            field_key="vendor_tax_id",
            user=self.admin,
            can_view=False,
            can_use_ai=False,
        )
        url = f"/api/workflows/invoices/{run.invoice.uuid}/"
        for suffix in ["", "fields/", "outputs/", "export/"]:
            self.assertEqual(self.client.get(url + suffix).status_code, 403)
        self.assertEqual(
            self.client.get(f"/api/workflows/runs/{run.uuid}/invoice/").status_code, 403
        )

    def test_export_only_field_restriction_preserves_view(self):
        run = self.advance(self.start())
        DocumentFieldPolicy.objects.create(
            document=self.document,
            field_key="vendor_name",
            user=self.admin,
            can_view=True,
            can_use_ai=True,
            can_export=False,
        )
        url = f"/api/workflows/invoices/{run.invoice.uuid}/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url + "export/").status_code, 403)

    def test_permission_revocation_stops_worker(self):
        run = self.start()
        self.admin.is_active = False
        self.admin.save(update_fields=["is_active"])
        with patch.object(InvoiceExtractionService, "extract") as extractor:
            self.advance(run)
            extractor.assert_not_called()
        self.assertEqual(run.status, "failed")

    def test_department_member_cannot_access_private_invoice(self):
        department = Department.objects.create(
            company=self.company, name="Private finance"
        )
        self.document.department = department
        self.document.save()
        run = self.advance(self.start())
        self.client.force_authenticate(self.member)
        self.assertEqual(
            self.client.get(f"/api/workflows/invoices/{run.invoice.uuid}/").status_code,
            404,
        )

    def test_cross_tenant_source_is_rejected(self):
        document = self.make_document(company=self.other)
        with self.assertRaises(PermissionDenied):
            self.start(documents=[document])

    def test_duplicate_and_vendor_matching_are_tenant_scoped(self):
        invoice = self.advance(self.start()).invoice
        foreign_doc = self.make_document(company=self.other)
        # Controlled foreign fixtures share index signals but remain inaccessible.
        _, version = InvoiceDefinitionService.create(
            self.foreign,
            name="Foreign",
            code="foreign",
            reviewer={"user": self.foreign.pk},
            policy={"require_approval": False},
        )
        foreign_run, _ = WorkflowEngineService.start(
            self.foreign,
            version_uuid=version.uuid,
            document_uuids=[str(foreign_doc.uuid)],
            idempotency_key="foreign-invoice",
        )
        self.advance(foreign_run)
        self.assertEqual(InvoiceMatchingService.duplicates(invoice, self.admin), [])
        InvoiceVendor.objects.create(company=self.other, name="Acme", identifier="0007")
        self.assertEqual(
            InvoiceMatchingService.vendor(invoice)["status"], "not_matched"
        )

    def test_required_vendor_ambiguity_routes_to_review(self):
        for _ in range(2):
            InvoiceVendor.objects.create(
                company=self.company, name="Acme", identifier="0007"
            )
        run = self.advance(self.start(self.version(require_vendor=True)))
        self.assertEqual(run.status, "review_required")
        self.assertTrue(run.findings.filter(category="vendor_ambiguous").exists())
        with self.assertRaises(ValidationError):
            self.accept(run)

    def test_unavailable_po_is_optional_unless_policy_requires_it(self):
        self.document.text_extraction.extracted_text += "PO number: PO-1\n"
        self.document.text_extraction.save()
        run = self.advance(self.start())
        self.assertEqual(run.status, "completed")
        self.assertEqual(
            run.invoice.matches["purchase_order"]["status"], "integration_unavailable"
        )
        second = self.make_document(self.TEXT + "PO number: PO-2\n")
        run = self.advance(
            self.start(self.version(require_po=True), documents=[second])
        )
        self.assertEqual(run.status, "review_required")
        self.assertTrue(run.findings.filter(category="po_unmatched").exists())

    def test_po_rejects_cross_tenant_and_invalid_evidence_results(self):
        invoice = self.advance(self.start()).invoice
        invoice.normalized["purchase_order_number"] = "PO-1"
        policy = InvoicePolicy.validate({"po_adapter": "test"})

        class Adapter:
            result = {}

            @classmethod
            def match(cls, **kwargs):
                return cls.result

        base = {
            "company_id": self.company.pk,
            "status": "matched",
            "reference": "PO-1",
            "currency": "USD",
            "amount": "110.00",
        }
        with patch.dict(
            InvoiceMatchingService.purchase_order_adapters, {"test": Adapter}
        ):
            for changes in [
                {"company_id": self.other.pk},
                {"document_uuids": [str(uuid.uuid4())]},
                {"reference": None},
                {"amount": "NaN"},
                {"status": []},
            ]:
                Adapter.result = {**base, **changes}
                with self.subTest(changes=changes), self.assertRaises(ValidationError):
                    InvoiceMatchingService.purchase_order(invoice, self.admin, policy)

    def test_invoice_correction_endpoint_requires_revision_and_preserves_lineage(self):
        run = self.advance(self.start(self.version(force_review=True)))
        field = run.invoice.extraction_run.result_fields.get(field_key="vendor_name")
        self.client.force_authenticate(self.reviewer)
        url = f"/api/workflows/extraction-runs/{field.extraction_run.uuid}/fields/{field.uuid}/correct/"
        self.assertEqual(
            self.client.patch(
                url, {"corrected_value": "Acme Limited"}, format="json"
            ).status_code,
            400,
        )
        response = self.client.patch(
            url,
            {
                "corrected_value": "Acme Limited",
                "expected_revision": run.revision,
                "reason": "Verified",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        field.refresh_from_db()
        self.assertEqual(field.extracted_value, "Acme")
        self.assertEqual(field.corrections.count(), 1)
        self.assertEqual(
            self.client.post(
                f"/api/workflows/extraction-runs/{field.extraction_run.uuid}/rerun/"
            ).status_code,
            403,
        )

    def test_line_items_have_per_cell_provenance(self):
        self.document.text_extraction.extracted_text += "description|quantity|unit_price|line_subtotal|tax_rate|tax_amount|line_total\nService|2|50.00|100.00|10%|10.00|110.00\n"
        self.document.text_extraction.save()
        run = self.advance(self.start())
        self.assertEqual(run.status, "completed")
        cells = InvoiceWorkflowService.provenance(run.invoice)["line_items"]["cells"]
        self.assertEqual(cells[0]["tax_rate"]["raw_value"], "10%")
        self.assertEqual(cells[0]["tax_rate"]["normalized_value"], "10")
        self.assertIn("Service|2", cells[0]["quantity"]["evidence"][0]["preview"])

    def ai_chunks(self):
        return [
            {
                "chunk_uuid": str(uuid.uuid4()),
                "document_uuid": str(self.document.uuid),
                "governance_revision": self.document.governance_revision,
                "content": self.TEXT,
            }
        ]

    def advance_to_extraction(self, run):
        for _ in range(2):
            WorkflowExecutorService.execute(run.company_id, str(run.uuid), run.revision)
            run.refresh_from_db()
        self.assertEqual(run.current_step.key, "extract_invoice_fields")

    def test_ai_malformed_output_rolls_back_fields_without_retry(self):
        run = self.start(self.version(use_ai=True))
        self.advance_to_extraction(run)
        chunks = self.ai_chunks()
        with patch(
            "apps.workflows.services.workflow_ai_service.VectorSearchService.search",
            return_value=chunks,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMGatewayService.generate_answer",
            return_value={"answer": "not JSON"},
        ):
            self.advance(run)
        self.assertEqual(run.status, "failed")
        self.assertIsNone(run.invoice.extraction_run_id)

    def test_ai_timeout_rolls_back_then_retry_succeeds(self):
        run = self.start(self.version(use_ai=True))
        self.advance_to_extraction(run)
        revision = run.revision
        chunks = self.ai_chunks()
        with patch(
            "apps.workflows.services.workflow_ai_service.VectorSearchService.search",
            return_value=chunks,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMGatewayService.generate_answer",
            side_effect=TimeoutError("private source data"),
        ):
            with self.assertRaises(TimeoutError):
                WorkflowExecutorService.execute(run.company_id, str(run.uuid), revision)
        run.refresh_from_db()
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.revision, revision)
        self.assertIsNone(run.invoice.extraction_run_id)
        with patch(
            "apps.workflows.services.workflow_ai_service.VectorSearchService.search",
            return_value=chunks,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMGatewayService.generate_answer",
            return_value={"answer": '{"fields":{}}'},
        ):
            self.advance(run)
        self.assertEqual(run.status, "completed")

    def test_sensitive_ai_routes_privately_with_invoice_schema_prompt(self):
        from apps.ai_engine.models import LLMProviderConfig

        self.document.sensitivity_label = "restricted"
        self.document.save(update_fields=["sensitivity_label"])
        run = self.start(self.version(use_ai=True))
        chunks = self.ai_chunks()
        response = {
            "fields": {
                "invoice_number": {
                    "raw_value": "000123",
                    "confidence": 0.95,
                    "evidence": [chunks[0]["chunk_uuid"]],
                }
            }
        }
        cloud = LLMProviderConfig(company=self.company, provider_type="openai")
        with patch(
            "apps.workflows.services.workflow_ai_service.VectorSearchService.search",
            return_value=chunks,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMProviderConfigService.get_default_config_for_company",
            return_value=cloud,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMGatewayService.generate_answer",
            return_value={"answer": json.dumps(response)},
        ) as gateway:
            self.advance(run)
        self.assertEqual(
            gateway.call_args.kwargs["provider_config"].provider_type, "ollama"
        )
        self.assertNotIn(
            'Return only JSON: {"findings"', gateway.call_args.kwargs["prompt"]
        )
        proof = InvoiceWorkflowService.provenance(run.invoice)["invoice_number"]
        self.assertEqual(proof["evidence"][0]["chunk_uuid"], chunks[0]["chunk_uuid"])

    def test_firewall_block_prevents_model_call(self):
        from apps.data_firewall.models import FirewallScan

        run = self.start(self.version(use_ai=True))
        self.advance_to_extraction(run)
        FirewallScan.objects.create(
            document=self.document,
            decision="block_cloud_llm",
            requires_human_approval=True,
        )
        with patch(
            "apps.workflows.services.workflow_ai_service.VectorSearchService.search",
            return_value=self.ai_chunks(),
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMGatewayService.generate_answer"
        ) as gateway:
            self.advance(run)
        self.assertEqual(run.status, "failed")
        gateway.assert_not_called()

    def test_definition_api_is_strict_and_tenant_owned(self):
        url = "/api/workflows/invoices/definitions/"
        body = {
            "name": "Invoice API",
            "code": "invoice-api",
            "reviewer": {"user": self.reviewer.pk},
            "department": self.department.pk,
        }
        self.assertEqual(
            self.client.post(
                url, {**body, "company": self.other.pk}, format="json"
            ).status_code,
            400,
        )
        response = self.client.post(url, body, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.client.post(url, body, format="json").status_code, 400)
        self.client.force_authenticate(self.member)
        self.assertEqual(
            self.client.post(
                url, {**body, "code": "denied"}, format="json"
            ).status_code,
            403,
        )

    def test_empty_ocr_creates_recoverable_extraction_exception(self):
        document = self.make_document("")
        run = self.advance(self.start(documents=[document]))
        self.assertEqual(run.status, "exception")
        exception = run.exceptions.get(status="open")
        self.assertEqual(exception.category, "extraction_failed")
        self.assertEqual(run.invoice.extraction_run.result_fields.count(), 23)
        self.assertFalse(run.invoice.outputs.exists())

    def test_financial_findings_must_be_reviewed_individually(self):
        self.document.text_extraction.extracted_text += "description|quantity|unit_price|line_subtotal|line_total\nA|1|50.00|40.00|50.00\nB|1|50.00|60.00|50.00\n"
        self.document.text_extraction.save()
        run = self.advance(self.start())
        discrepancies = list(run.findings.filter(category="line_subtotal_mismatch"))
        self.assertEqual(len(discrepancies), 2)
        for finding in run.findings.exclude(category="line_subtotal_mismatch"):
            WorkflowEngineService.review_finding(
                self.reviewer, run, finding.uuid, status="accepted"
            )
        WorkflowEngineService.review_finding(
            self.reviewer, run, discrepancies[0].uuid, status="accepted"
        )
        with self.assertRaises(ValidationError):
            self.accept(run)
        WorkflowEngineService.review_finding(
            self.reviewer, run, discrepancies[1].uuid, status="accepted"
        )
        run = self.advance(self.accept(run))
        self.assertEqual(run.status, "completed")

    def test_seeded_template_is_immutable_even_for_platform_admin(self):
        from apps.workflows.models import ExtractionTemplate

        template = ExtractionTemplate.objects.get(
            name="Canonical Invoice Schema v1", is_system=True
        )
        self.admin.role = "platform_admin"
        self.admin.save(update_fields=["role"])
        url = f"/api/workflows/extraction-templates/{template.uuid}/"
        self.assertEqual(
            self.client.patch(url, {"name": "Changed"}, format="json").status_code, 403
        )
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.assertEqual(
            self.client.post(
                url + "fields/",
                {"field_key": "injected", "label": "Injected"},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            template.fields.get(field_key="grand_total").field_type, "amount"
        )

    def test_invoice_definition_cannot_skip_validation_or_mix_policies(self):
        from apps.workflows.services.workflow_definition_service import (
            WorkflowDefinitionService,
        )

        version = self.version()
        steps = [
            {
                "key": step.key,
                "name": step.name,
                "step_type": step.step_type,
                "configuration": step.configuration,
            }
            for step in version.steps.all()
        ]
        transitions = [
            {
                "source": edge.source.key,
                "target": edge.target.key,
                "outcome": edge.outcome,
            }
            for edge in version.transitions.select_related("source", "target")
        ]
        for changes in ["policy", "rule"]:
            altered = json.loads(json.dumps(steps))
            for step in altered:
                if changes == "policy" and step["key"] == "generate_normalized_invoice":
                    step["configuration"]["options"]["require_approval"] = True
                if changes == "rule" and step["key"] == "review_decision":
                    step["configuration"]["value"] = False
            with self.assertRaises(ValidationError):
                WorkflowDefinitionService.publish(
                    self.admin,
                    version.definition,
                    entry_step=version.entry_step,
                    steps=altered,
                    transitions=transitions,
                )
        self.assertEqual(version.definition.versions.count(), 1)

    def test_final_output_cannot_be_created_before_review(self):
        run = self.advance(self.start(self.version(force_review=True)))
        with self.assertRaises(ValidationError):
            InvoiceWorkflowService.finalize(
                run.invoice,
                InvoicePolicy.validate(
                    {"require_approval": False, "force_review": True}
                ),
            )
        self.assertFalse(run.invoice.outputs.exists())

    def test_audit_and_notifications_do_not_contain_invoice_values(self):
        from apps.notifications.models import Notification

        run = self.advance(self.start(self.version(force_review=True)))
        events = list(
            AuditLog.objects.filter(resource_uuid=run.uuid).values_list(
                "metadata", flat=True
            )
        )
        messages = list(
            Notification.objects.filter(resource_uuid=run.uuid).values_list(
                "message", flat=True
            )
        )
        self.assertTrue(events)
        self.assertTrue(messages)
        self.assertNotIn("000123", json.dumps(events))
        self.assertNotIn("Acme", json.dumps(events))
        self.assertNotIn("000123", json.dumps(messages))
        self.assertNotIn("110.00", json.dumps(messages))

    def test_revocation_during_model_call_prevents_field_persistence(self):
        run = self.start(self.version(use_ai=True))
        chunks = self.ai_chunks()

        def revoke(**kwargs):
            DocumentFieldPolicy.objects.create(
                document=self.document,
                field_key="vendor_tax_id",
                user=self.admin,
                can_view=True,
                can_use_ai=False,
            )
            return {"answer": '{"fields":{}}'}

        with patch(
            "apps.workflows.services.workflow_ai_service.VectorSearchService.search",
            return_value=chunks,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMGatewayService.generate_answer",
            side_effect=revoke,
        ):
            self.advance(run)
        self.assertEqual(run.status, "failed")
        self.assertIsNone(run.invoice.extraction_run_id)

    def test_real_pdf_page_markers_and_invalid_pages(self):
        extraction = self.document.text_extraction
        extraction.page_count = 2
        extraction.save()
        text = "[Page 1]\nHeader\n[Page 2]\nInvoice: 0007"
        self.assertEqual(
            evidence(self.document, text, text.index("0007"), len(text), "pdf_text")[
                "page"
            ],
            2,
        )
        with self.assertRaises(ValidationError):
            evidence(self.document, "[Page 3]\n0007", 9, 13, "pdf_text")

    def test_duplicate_candidates_respect_workflow_department_access(self):
        private = Department.objects.create(
            company=self.company, name="Private invoice workflow"
        )
        _, version = InvoiceDefinitionService.create(
            self.admin,
            name="Private",
            code="private-invoice",
            department=private.pk,
            reviewer={"user": self.admin.pk},
            policy={"require_approval": False},
        )
        first = self.advance(self.start(version))
        self.assertEqual(first.status, "completed")
        second = self.advance(self.start(documents=[self.make_document()]))
        self.assertTrue(second.invoice.duplicates)
        self.assertEqual(
            InvoiceMatchingService.duplicates(second.invoice, self.reviewer), []
        )
        self.client.force_authenticate(self.reviewer)
        response = self.client.get(f"/api/workflows/invoices/{second.invoice.uuid}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["duplicate_candidates"], [])
