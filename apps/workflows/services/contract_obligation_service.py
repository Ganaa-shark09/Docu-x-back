"""Human assignment of explicitly evidenced, frozen contract-review obligations.

This is not contract execution, an automatic legal determination, or a deadline
scheduler. It deliberately never copies client-provided legal facts into records.
"""

import hashlib
import json

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.workflows.models import (
    ContractObligationAssignment,
    ContractObligationEvent,
    ContractReview,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services.contract_extraction_service import validate_evidence
from apps.workflows.services.workflow_engine_service import WorkflowConflict
from apps.workflows.services.workflow_events import workflow_event


class ContractObligationService:
    @staticmethod
    def _proof(review, row):
        proof = row.get("evidence")
        if not proof and review.extraction_run_id:
            field = review.extraction_run.result_fields.filter(
                field_key="obligations", is_deleted=False
            ).first()
            if field:
                metadata = field.source_metadata if isinstance(field.source_metadata, dict) else {}
                proof = (
                    metadata.get("review_evidence")
                    if field.is_corrected
                    else metadata.get("evidence")
                )
        if not proof:
            raise ValidationError("Obligation has no source proof; review it first.")
        validate_evidence(review.document, proof)
        return proof

    @classmethod
    def _verified_row(cls, review, index):
        rows = review.normalized.get("obligations") if isinstance(review.normalized, dict) else None
        if not isinstance(rows, list) or index >= min(len(rows), 100):
            raise ValidationError({"source_index": "No obligation at this source index."})
        row = rows[index]
        if not isinstance(row, dict) or not isinstance(row.get("action"), str) or not row["action"].strip():
            raise ValidationError("Obligation text is not verified.")
        proof = cls._proof(review, row)
        # Persist server-authoritative data, never a text/date/evidence from a client.
        return {"party": row.get("party"), "action": row["action"],
                "timing": row.get("timing"), "timing_type": row.get("timing_type"),
                "due_date": row.get("due_date"), "condition": row.get("condition"),
                "frequency": row.get("frequency"), "evidence": proof[:20]}

    @staticmethod
    def _digest(snapshot):
        return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=True).encode()).hexdigest()

    @staticmethod
    def _authorize_owner(user, review, owner_id):
        owner = User.objects.filter(
            pk=owner_id, company_id=user.company_id, is_active=True
        ).select_related("company").first()
        if not owner or not WorkflowPermissionService.content_allowed(owner, review.workflow_run):
            raise ValidationError({"owner_id": "Choose an active authorized owner."})
        return owner

    @classmethod
    @transaction.atomic
    def assign(cls, user, review, *, source_index, owner_id, expected_review_revision):
        WorkflowPermissionService.require(user, review.workflow_run, "assign")
        review = ContractReview.objects.select_for_update(of=("self",)).select_related(
            "document", "document__text_extraction", "workflow_run", "extraction_run"
        ).get(pk=review.pk)
        if not WorkflowPermissionService.content_allowed(user, review.workflow_run):
            raise PermissionDenied("Current contract permissions deny assignment.")
        if review.revision != expected_review_revision:
            raise WorkflowConflict("Contract revision changed. Reload the review.")
        if (review.workflow_run.status != "completed" or not review.outputs.filter(
            revision=review.revision, is_deleted=False
        ).exists()):
            raise ValidationError("Obligations require a completed, source-reviewed output.")
        original_governance = review.workflow_run.document_links.filter(
            document_id=review.document_id
        ).values_list("governance_revision", flat=True).first()
        if original_governance != review.document.governance_revision:
            raise WorkflowConflict("Contract source governance changed; start a fresh review.")
        owner = cls._authorize_owner(user, review, owner_id)
        snapshot = cls._verified_row(review, source_index)
        if ContractObligationAssignment.objects.filter(
            review=review, source_revision=review.revision, source_index=source_index
        ).exists():
            raise WorkflowConflict("Obligation already assigned; reload its state.")
        item = ContractObligationAssignment.objects.create(
            company=review.company, review=review, owner=owner,
            created_by=user, source_revision=review.revision,
            source_index=source_index, source_digest=cls._digest(snapshot),
            source_snapshot=snapshot,
            governance_revision=review.document.governance_revision,
        )
        ContractObligationEvent.objects.create(
            obligation=item, actor=user, sequence=1, to_status=item.status,
            reason="Assigned for human follow-up",
        )
        workflow_event(review.workflow_run, user, "contract_obligation_assigned",
                       obligation_uuid=str(item.uuid), source_revision=review.revision,
                       source_index=source_index, owner_id=owner.pk)
        return item

    @classmethod
    @transaction.atomic
    def transition(cls, user, review, *, item_uuid, status, reason, expected_revision):
        item = ContractObligationAssignment.objects.select_for_update().select_related(
            "review", "review__document", "review__workflow_run", "owner"
        ).filter(uuid=item_uuid, review_id=review.pk, company_id=user.company_id,
                 is_deleted=False).first()
        if item is None:
            from django.http import Http404
            raise Http404
        if not WorkflowPermissionService.content_allowed(user, item.review.workflow_run):
            raise PermissionDenied("Current contract permissions deny this action.")
        if (user.pk != item.owner_id
                and not WorkflowPermissionService.can(user, item.review.workflow_run, "assign")):
            raise PermissionDenied("Only the owner or an authorized manager may update progress.")
        if (item.review.revision != item.source_revision
                or item.review.document.governance_revision != item.governance_revision):
            raise WorkflowConflict("Source or governance revision changed; reassess this obligation.")
        if (item.review.revision == item.source_revision
                and cls._digest(cls._verified_row(item.review, item.source_index))
                != item.source_digest):
            raise WorkflowConflict("Verified obligation source changed; reassess before updating.")
        if item.revision != expected_revision:
            raise WorkflowConflict("Obligation revision changed; reload before updating.")
        valid_next = {
            "open": {"in_progress", "completed"},
            "in_progress": {"completed"},
            "completed": set(),
        }
        if status not in valid_next[item.status]:
            raise ValidationError({"status": "Unsupported obligation transition."})
        old_status = item.status
        item.status = status
        item.revision += 1
        item.save(update_fields=["status", "revision", "updated_at"])
        ContractObligationEvent.objects.create(
            obligation=item, actor=user, sequence=item.revision,
            from_status=old_status, to_status=status, reason=reason,
        )
        workflow_event(item.review.workflow_run, user, "contract_obligation_updated",
                       obligation_uuid=str(item.uuid), status=status,
                       revision=item.revision)
        return item
