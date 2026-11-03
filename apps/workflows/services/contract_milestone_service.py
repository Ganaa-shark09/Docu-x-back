"""Read-only, evidence-linked contract date projection for the review workspace.

This is NOT an obligation scheduler, legal opinion, or inferred deadline engine.
Only date values with source evidence appear; relative date math is never guessed.
"""

from datetime import date


class ContractMilestoneService:
    DATE_FIELDS = (
        ("execution_date", "Execution"),
        ("effective_date", "Effective"),
        ("start_date", "Start"),
        ("expiration_date", "Expiration"),
        ("renewal_date", "Renewal"),
        ("notice_deadline", "Notice deadline"),
    )

    @staticmethod
    def canonical_date(value):
        if not isinstance(value, str) or len(value) != 10:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
        return value if parsed.isoformat() == value else None

    @classmethod
    def snapshot(cls, review):
        normalized = review.normalized if isinstance(review.normalized, dict) else {}
        source_fields = (
            {
                field.field_key: field
                for field in review.extraction_run.result_fields.filter(
                    is_deleted=False
                )
            }
            if review.extraction_run_id else {}
        )
        items = []
        document_uuid = str(review.document.uuid)

        def verified_proofs(proofs):
            if not isinstance(proofs, list):
                return []
            return [
                proof
                for proof in proofs[:20]
                if isinstance(proof, dict)
                and proof.get("document_uuid") == document_uuid
            ]

        def proof_for(key):
            field = source_fields.get(key)
            if field is None:
                return []
            metadata = (
                field.source_metadata
                if isinstance(field.source_metadata, dict)
                else {}
            )
            proof = (
                metadata.get("review_evidence")
                if field.is_corrected
                else metadata.get("evidence")
            )
            return verified_proofs(proof)

        for key, label in cls.DATE_FIELDS:
            when = cls.canonical_date(normalized.get(key))
            proof = proof_for(key)
            if when and proof:
                items.append({
                    "key": key,
                    "kind": key,
                    "label": label,
                    "date": when,
                    "source_field": key,
                    "source_index": None,
                    "evidence": proof,
                })

        # Each dated obligation or schedule entry is a separate sourced event;
        # never consolidate equal dates (different parties may owe distinct duties).
        for field_key, kind, date_key, label_key in (
            ("schedule_dates", "schedule", "date", "description"),
            ("obligations", "obligation_due", "due_date", "action"),
        ):
            rows = normalized.get(field_key)
            if not isinstance(rows, list):
                continue
            for index, row in enumerate(rows[:100]):
                if not isinstance(row, dict):
                    continue
                when = cls.canonical_date(row.get(date_key))
                proof = verified_proofs(row.get("evidence")) or proof_for(field_key)
                if not when or not proof:
                    continue
                label = row.get(label_key)
                items.append({
                    "key": f"{field_key}:{index}",
                    "kind": kind,
                    "label": (
                        label[:160]
                        if isinstance(label, str) and label
                        else kind.replace("_", " ").title()
                    ),
                    "date": when,
                    "source_field": field_key,
                    "source_index": index,
                    "evidence": proof[:20],
                })

        items.sort(key=lambda item: (item["date"], item["key"]))
        return {
            "contract_uuid": str(review.uuid),
            "revision": review.revision,
            "source_coverage_complete": (
                review.coverage.get("complete") is True
                if isinstance(review.coverage, dict)
                else False
            ),
            "review_output_finalized": review.outputs.filter(
                revision=review.revision, is_deleted=False
            ).exists(),
            "milestones": items,
        }
