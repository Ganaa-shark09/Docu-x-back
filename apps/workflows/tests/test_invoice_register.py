import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.documents.models import Document, DocumentFieldPolicy
from apps.tenants.models import Company
from apps.workflows.models import (
    InvoiceOutputVersion,
    InvoiceRecord,
    WorkflowRunDocument,
)
from apps.workflows.services.invoice_definition_service import InvoiceDefinitionService
from apps.workflows.services.workflow_engine_service import WorkflowEngineService


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
class InvoiceRegisterAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Register Co", slug="register-co")
        cls.other = Company.objects.create(
            name="Other Register Co", slug="other-register-co"
        )
        cls.admin = User.objects.create_user(
            username="invoice-register-admin", company=cls.company, role="company_admin"
        )
        cls.foreign = User.objects.create_user(
            username="invoice-register-foreign", company=cls.other, role="company_admin"
        )

    def setUp(self):
        self.enqueue_patch = patch.object(WorkflowEngineService, "enqueue")
        self.enqueue_patch.start()
        self.addCleanup(self.enqueue_patch.stop)
        self.client.force_authenticate(self.admin)

    def create_invoice(
        self, user=None, *, number="000123", vendor="Acme", status="queued"
    ):
        user = user or self.admin
        doc = Document.objects.create(
            document_type="invoice",
            company=user.company,
            uploaded_by=user,
            title=f"Source {number}",
            original_filename="invoice.txt",
            status="ready",
            is_ai_ready=True,
        )
        _, version = InvoiceDefinitionService.create(
            user,
            name=f"Register {number}",
            code=f"invoice-{uuid.uuid4().hex[:12]}",
            reviewer={"user": user.pk},
            policy={"require_approval": False},
        )
        run, _ = WorkflowEngineService.start(
            user,
            version_uuid=version.uuid,
            document_uuids=[str(doc.uuid)],
            idempotency_key=uuid.uuid4().hex,
        )
        if status != run.status:
            run.status = status
            run.save(update_fields=["status", "updated_at"])
        invoice = InvoiceRecord.objects.create(
            company=user.company,
            document=doc,
            workflow_run=run,
            invoice_number=number,
            invoice_date="2026-10-07",
            normalized={
                "vendor_name": vendor,
                "currency": "USD",
                "grand_total": "110.00",
            },
            validation=[{"code": "test_issue", "blocking": True}],
        )
        return invoice

    def test_paginated_invoice_register_and_safe_shape(self):
        invoice = self.create_invoice()
        response = self.client.get("/api/workflows/invoices/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertEqual(row["uuid"], str(invoice.uuid))
        self.assertEqual(row["invoice_number"], "000123")
        self.assertEqual(row["vendor_name"], "Acme")
        self.assertEqual(row["grand_total"], "110.00")
        self.assertEqual(row["open_issues"], 1)
        self.assertFalse(row["has_final_output"])
        self.assertIn("effective_confidence", row)
        self.assertIn("updated_at", row)
        self.assertNotIn("normalized", row)
        self.assertNotIn("field_provenance", row)
        self.assertNotIn("final_output", row)

    def test_search_status_validation_and_filtering(self):
        self.create_invoice(number="000123", vendor="Acme", status="review_required")
        self.create_invoice(number="000777", vendor="Beta", status="completed")
        self.assertEqual(
            self.client.get("/api/workflows/invoices/?status=review_required").data[
                "count"
            ],
            1,
        )
        self.assertEqual(
            self.client.get("/api/workflows/invoices/?search=Beta").data["count"], 1
        )
        self.assertEqual(
            self.client.get("/api/workflows/invoices/?search=000123").data["count"], 1
        )
        summary = self.client.get("/api/workflows/invoices/summary/")
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary["Cache-Control"], "no-store")
        self.assertEqual(summary.data["total"], 2)
        self.assertEqual(summary.data["by_status"]["completed"], 1)
        self.assertEqual(summary.data["by_status"]["review_required"], 1)
        self.assertEqual(
            self.client.get("/api/workflows/invoices/summary/?status=completed").data[
                "total"
            ],
            1,
        )
        self.assertEqual(
            self.client.get("/api/workflows/invoices/summary/?search=Beta").data[
                "total"
            ],
            1,
        )
        self.assertEqual(
            self.client.get(
                "/api/workflows/invoices/summary/?status=made_up"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                "/api/workflows/invoices/summary/?search=" + "x" * 101
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/api/workflows/invoices/?status=made_up").status_code, 400
        )
        self.assertEqual(
            self.client.get("/api/workflows/invoices/?search=" + "x" * 101).status_code,
            400,
        )

    def test_field_policy_and_retention_fail_closed(self):
        visible = self.create_invoice(number="visible")
        restricted = self.create_invoice(number="restricted")
        expired = self.create_invoice(number="expired")
        DocumentFieldPolicy.objects.create(
            document=restricted.document, field_key="vendor_name", can_view=False
        )
        expired.document.retention_start = timezone.now() - timedelta(days=3)
        expired.document.retention_expires_at = timezone.now() - timedelta(days=1)
        expired.document.save(
            update_fields=["retention_start", "retention_expires_at", "updated_at"]
        )
        response = self.client.get("/api/workflows/invoices/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["uuid"] for row in response.data["results"]], [str(visible.uuid)]
        )
        self.assertEqual(
            self.client.get(f"/api/workflows/invoices/{restricted.uuid}/").status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/workflows/invoices/summary/").data["total"], 1
        )

    def test_company_isolation_and_inactive_account(self):
        visible = self.create_invoice(number="ours")
        foreign = self.create_invoice(self.foreign, number="foreign")
        response = self.client.get("/api/workflows/invoices/?search=foreign")
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(
            self.client.get(f"/api/workflows/invoices/{foreign.uuid}/").status_code, 404
        )
        self.assertEqual(
            self.client.get("/api/workflows/invoices/").data["results"][0]["uuid"],
            str(visible.uuid),
        )
        self.assertEqual(
            self.client.get("/api/workflows/invoices/summary/").data["total"], 1
        )
        self.admin.is_active = False
        self.admin.save(update_fields=["is_active"])
        self.assertEqual(self.client.get("/api/workflows/invoices/").data["count"], 0)
        self.assertEqual(
            self.client.get("/api/workflows/invoices/summary/").data["total"], 0
        )

    def test_secondary_source_field_policy_blocks_register_and_summary(self):
        invoice = self.create_invoice(number="multi-source")
        second = Document.objects.create(
            document_type="invoice",
            company=self.company,
            uploaded_by=self.admin,
            title="Second source",
            original_filename="attachment.txt",
            status="ready",
            is_ai_ready=True,
        )
        WorkflowRunDocument.objects.create(
            run=invoice.workflow_run,
            document=second,
            governance_revision=second.governance_revision,
        )
        self.assertEqual(self.client.get("/api/workflows/invoices/").data["count"], 1)
        DocumentFieldPolicy.objects.create(
            document=second,
            field_key="bank_account",
            can_view=False,
        )
        self.assertEqual(self.client.get("/api/workflows/invoices/").data["count"], 0)
        self.assertEqual(
            self.client.get("/api/workflows/invoices/summary/").data["total"], 0
        )
        self.assertEqual(
            self.client.get(f"/api/workflows/invoices/{invoice.uuid}/").status_code, 403
        )

    def test_output_presence_is_listed_without_exposing_output_data(self):
        invoice = self.create_invoice(number="has-output")
        InvoiceOutputVersion.objects.create(
            invoice=invoice,
            revision=1,
            schema_version=1,
            data={"private": "should-not-appear"},
            digest="a" * 64,
        )
        response = self.client.get("/api/workflows/invoices/")
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["results"][0]
        self.assertTrue(row["has_final_output"])
        self.assertNotIn("private", str(row))
        self.assertNotIn("final_output", row)

    def test_batch_progress_does_not_report_queued_as_failed(self):
        self.create_invoice(number="fixture")
        doc = Document.objects.create(
            document_type="invoice",
            company=self.company,
            uploaded_by=self.admin,
            title="Still processing",
            original_filename="processing.pdf",
            status="processing",
        )
        version = InvoiceDefinitionService.create(
            self.admin,
            name="Batch register",
            code="invoice-batch-register",
            reviewer={"user": self.admin.pk},
            policy={"require_approval": False},
        )[1]
        response = self.client.post(
            "/api/workflows/invoice-batches/",
            {
                "name": "Queued invoices",
                "version": str(version.uuid),
                "documents": [str(doc.uuid)],
                "idempotency_key": uuid.uuid4().hex,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["items"][0]["status"], "queued")
        self.assertEqual(response.data["progress"]["processing"], 1)
        self.assertEqual(response.data["progress"]["failed"], 0)
