import copy
import uuid

from django.test import SimpleTestCase

from apps.workflows.serializers.contradiction_serializers import (
    ContradictionJobCreateSerializer,
    ContradictionReviewSerializer,
)


class ContradictionContractTests(SimpleTestCase):
    def payload(self):
        return {
            "subject": "Contract ACME-001 / 2026",
            "pairs": [
                {
                    "left": {"claim_uuid": str(uuid.uuid4()), "revision": 1},
                    "right": {"claim_uuid": str(uuid.uuid4()), "revision": 1},
                    "field": "payment term",
                    "kind": "value",
                    "basis": "across_documents",
                    "units": "days",
                }
            ],
            "idempotency_key": "contract-check",
        }

    def test_explicit_pair_contract(self):
        form = ContradictionJobCreateSerializer(data=self.payload())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.validated_data["department"])

    def test_server_owned_fields_rejected(self):
        for key in ("company", "confidence", "severity", "status", "findings"):
            payload = self.payload()
            payload[key] = "forged"
            self.assertFalse(ContradictionJobCreateSerializer(data=payload).is_valid())

    def test_nested_unknown_fields_rejected(self):
        for target in ("pair", "reference"):
            payload = self.payload()
            node = payload["pairs"][0]
            if target == "reference":
                node = node["left"]
            node["proof_status"] = "verified"
            self.assertFalse(ContradictionJobCreateSerializer(data=payload).is_valid())

    def test_bounds_and_required_subject(self):
        for count in (0, 21):
            payload = self.payload()
            payload["pairs"] *= count
            self.assertFalse(ContradictionJobCreateSerializer(data=payload).is_valid())
        payload = self.payload()
        payload["subject"] = " "
        self.assertFalse(ContradictionJobCreateSerializer(data=payload).is_valid())

    def test_same_revision_and_reversed_duplicates_rejected(self):
        payload = self.payload()
        payload["pairs"][0]["right"] = payload["pairs"][0]["left"]
        self.assertFalse(ContradictionJobCreateSerializer(data=payload).is_valid())
        payload = self.payload()
        second = copy.deepcopy(payload["pairs"][0])
        second["left"], second["right"] = second["right"], second["left"]
        payload["pairs"].append(second)
        self.assertFalse(ContradictionJobCreateSerializer(data=payload).is_valid())

    def test_version_pair_requires_distinct_revisions(self):
        payload = self.payload()
        pair = payload["pairs"][0]
        pair["right"] = {**pair["left"], "revision": 2}
        pair["basis"] = "across_versions"
        self.assertTrue(ContradictionJobCreateSerializer(data=payload).is_valid())
        pair["basis"] = "across_documents"
        self.assertFalse(ContradictionJobCreateSerializer(data=payload).is_valid())

    def test_invalid_comparison_or_revision_rejected(self):
        for key, value in (("kind", "llm_guess"), ("basis", "anywhere")):
            payload = self.payload()
            payload["pairs"][0][key] = value
            self.assertFalse(ContradictionJobCreateSerializer(data=payload).is_valid())
        payload = self.payload()
        payload["pairs"][0]["left"]["revision"] = 0
        self.assertFalse(ContradictionJobCreateSerializer(data=payload).is_valid())

    def test_resolution_requires_reason_revision_and_explanation(self):
        payload = {
            "revision": 1,
            "decision": "resolved",
            "reason": "Checked both sources",
            "idempotency_key": "review-1",
        }
        self.assertFalse(ContradictionReviewSerializer(data=payload).is_valid())
        payload["resolution"] = "Document owner corrected the effective date"
        self.assertTrue(ContradictionReviewSerializer(data=payload).is_valid())
        payload["confidence"] = 1
        self.assertFalse(ContradictionReviewSerializer(data=payload).is_valid())
