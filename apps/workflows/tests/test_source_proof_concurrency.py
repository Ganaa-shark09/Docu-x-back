import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.workflows.models.source_proof import EvidenceRecord
from apps.workflows.services.source_proof_service import SourceProofService as proof
from apps.workflows.services.workflow_engine_service import WorkflowConflict
from apps.workflows.tests import test_source_proof as fixtures


@override_settings(MEDIA_ROOT="/tmp/docux-source-proof-concurrency")
class SourceProofConcurrencyTests(TransactionTestCase):
    capture = fixtures.SourceProofTests.capture
    claim = fixtures.SourceProofTests.claim

    def setUp(self):
        fixtures.SourceProofTests.setUpTestData.__func__(type(self))
        self.client = APIClient()
        fixtures.SourceProofTests.setUp(self)

    def race(self, operation):
        barrier = threading.Barrier(2)

        def worker(index):
            close_old_connections()
            try:
                user = User.objects.get(pk=self.admin.pk)
                barrier.wait(timeout=15)
                return operation(user, index)
            except (WorkflowConflict, ValidationError):
                return "denied"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(worker, [0, 1]))

    def test_duplicate_evidence_capture_one_identity(self):
        values = dict(
            kind="document",
            source_uuid=self.doc.uuid,
            locator_type="text_offsets",
            locator={"document_uuid": str(self.doc.uuid), "start": 7, "end": 15},
            precision="exact",
        )
        result = self.race(lambda user, _: proof.capture(user, **values).pk)
        self.assertEqual(result[0], result[1])
        self.assertEqual(EvidenceRecord.objects.count(), 1)

    def test_concurrent_corrections_one_revision_winner(self):
        claim, _ = self.claim()
        result = self.race(
            lambda user, index: proof.correct(
                user, claim, 1, str(index), "Checked record", "fix" + str(index)
            ).pk
        )
        self.assertEqual(result.count("denied"), 1)
        self.assertEqual(claim.versions.count(), 2)

    def test_duplicate_correction_replays_without_duplicate_revision(self):
        claim, _ = self.claim()
        result = self.race(
            lambda user, _: proof.correct(
                user, claim, 1, "12000", "Checked record", "same"
            ).pk
        )
        self.assertEqual(result[0], result[1])
        self.assertEqual(claim.versions.count(), 2)

    def test_concurrent_review_one_revision_winner(self):
        row = self.capture()
        result = self.race(
            lambda user, index: proof.review(
                user, row, 0, "confirmed", "Checked", "review" + str(index)
            ).pk
        )
        self.assertEqual(result.count("denied"), 1)
        self.assertEqual(row.reviews.count(), 1)

    def test_duplicate_validation_does_not_duplicate_history(self):
        row = self.capture()
        result = self.race(lambda user, _: proof.validate(user, row).pk)
        self.assertEqual(result[0], result[1])
        self.assertEqual(row.validations.count(), 1)

    def test_concurrent_claim_normalization_one_identity(self):
        row = self.capture()
        result = self.race(
            lambda user, _: proof.claim(user, row, "total", "12500.00").pk
        )
        self.assertEqual(result[0], result[1])
