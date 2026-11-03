"""PostgreSQL regression: lock review itself, not nullable joined relations.

The contract-obligation assignment endpoint is exercised by the four Phase 2
ContractWorkflowTests. This test additionally checks the exact join/lock shape
that previously produced PostgreSQL's FeatureNotSupported error.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.workflows.models import ContractReview


class ContractObligationRowLockTests(TestCase):
    def test_review_row_lock_works_with_optional_eager_relations(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL-specific FOR UPDATE OF semantics")

        # A nonexistent ID is enough: PostgreSQL validates the locking clause
        # even if the query returns no rows. TestCase provides a transaction.
        with CaptureQueriesContext(connection) as captured:
            rows = list(
                ContractReview.objects.select_for_update(of=("self",))
                .select_related(
                    "document", "document__text_extraction",
                    "workflow_run", "extraction_run",
                )
                .filter(pk=-1)
            )
        self.assertEqual(rows, [])
        self.assertTrue(
            any("FOR UPDATE OF" in item["sql"].upper() for item in captured),
            "Expected PostgreSQL to lock only the ContractReview table",
        )
