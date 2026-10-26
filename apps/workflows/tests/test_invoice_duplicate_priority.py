"""Invoice duplicate-ranking regression tests.

Keep fixtures tenant-local and never perform real AI inference or ERP posting.
"""

import uuid
from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.documents.models import Document
from apps.tenants.models import Company
from apps.workflows.models import InvoiceRecord
from apps.workflows.services.invoice_definition_service import InvoiceDefinitionService
from apps.workflows.services.invoice_matching_service import InvoiceMatchingService
from apps.workflows.services.workflow_engine_service import WorkflowEngineService


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
class InvoiceDuplicatePriorityTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Duplicate Ranking", slug="duplicate-ranking"
        )
        cls.admin = User.objects.create_user(
            username="duplicate-ranking-admin",
            company=cls.company,
            role="company_admin",
        )
        _, cls.version = InvoiceDefinitionService.create(
            cls.admin,
            name="Duplicate Priority",
            code="duplicate-priority",
            reviewer={"user": cls.admin.pk},
            policy={"require_approval": False},
        )

    def setUp(self):
        enqueue_patch = patch.object(WorkflowEngineService, "enqueue")
        enqueue_patch.start()
        self.addCleanup(enqueue_patch.stop)

    def create_invoice(self, *, number, vendor="acme", checksum=""):
        doc = Document.objects.create(
            document_type="invoice",
            company=self.company,
            uploaded_by=self.admin,
            title="Invoice source",
            original_filename="invoice.txt",
            status="ready",
            is_ai_ready=True,
            checksum=checksum,
        )
        run, _ = WorkflowEngineService.start(
            self.admin,
            version_uuid=self.version.uuid,
            document_uuids=[str(doc.uuid)],
            idempotency_key=uuid.uuid4().hex,
        )
        return InvoiceRecord.objects.create(
            company=self.company,
            workflow_run=run,
            document=doc,
            invoice_number=number,
            vendor_key=vendor,
        )

    def test_new_exact_checksum_is_not_hidden_behind_fifty_probables(self):
        subject = self.create_invoice(number="INV-500", checksum="same-source-digest")
        probables = [self.create_invoice(number="INV-500") for _ in range(51)]
        exact = self.create_invoice(
            number="UNRELATED-NUMBER",
            vendor="different-vendor",
            checksum="same-source-digest",
        )

        matches = InvoiceMatchingService.duplicates(subject, self.admin)

        self.assertEqual(len(matches), 50)
        self.assertEqual(matches[0]["invoice_uuid"], str(exact.uuid))
        self.assertEqual(matches[0]["match_strength"], "exact")
        self.assertIn("document_checksum", matches[0]["signals"])
        self.assertTrue(all(row["status"] == "candidate" for row in matches))
        self.assertNotIn(
            str(probables[0].uuid), [row["invoice_uuid"] for row in matches]
        )

    def test_probable_matches_are_ordered_newest_first(self):
        subject = self.create_invoice(number="INV-600")
        candidates = [self.create_invoice(number="INV-600") for _ in range(3)]

        matches = InvoiceMatchingService.duplicates(subject, self.admin)

        self.assertEqual(
            [row["invoice_uuid"] for row in matches],
            [str(row.uuid) for row in reversed(candidates)],
        )
        self.assertTrue(all(row["match_strength"] == "probable" for row in matches))
        self.assertTrue(
            all(
                {"invoice_number", "vendor_identity"}.issubset(row["signals"])
                for row in matches
            )
        )
