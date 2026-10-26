from celery import shared_task

from apps.accounts.models import User
from apps.workflows.models.source_proof import EvidenceRecord
from apps.workflows.services.proof_source_service import reference
from apps.workflows.services.source_proof_service import SourceProofService


@shared_task(acks_late=True, reject_on_worker_lost=True)
def revalidate_source_evidence(company_id, actor_id, evidence_uuids):
    """Explicit bounded batches only; cached validity never grants source access."""
    if not isinstance(evidence_uuids, list) or not 1 <= len(evidence_uuids) <= 100:
        return False
    user = User.objects.filter(
        pk=actor_id, company_id=company_id, is_active=True
    ).first()
    if not user:
        return False
    ids = [reference(value) for value in evidence_uuids]
    rows = EvidenceRecord.objects.filter(
        uuid__in=ids, source__company_id=company_id, is_deleted=False
    ).order_by("pk")
    for row in rows:
        SourceProofService.validate(user, row)
    return True
