from celery import shared_task
from django.db.models import Exists, OuterRef

from apps.workflows.models import InvoiceBatch, InvoiceBatchItem
from apps.workflows.services.invoice_batch_service import InvoiceBatchService


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def process_invoice_batch(self, company_id, batch_uuid):
    try:
        waiting = InvoiceBatchService.start_waiting(company_id, batch_uuid)
    except Exception:
        raise self.retry(
            exc=RuntimeError("Invoice batch dispatch temporarily unavailable."),
            countdown=2 ** (self.request.retries + 1),
        ) from None
    if waiting:
        process_invoice_batch.apply_async(args=[company_id, batch_uuid], countdown=10)
    return waiting


@shared_task
def dispatch_waiting_invoice_batches():
    pending = InvoiceBatchItem.objects.filter(
        batch_id=OuterRef("pk"),
        error_code="awaiting_document",
        run__isnull=True,
        is_deleted=False,
    )
    batches = (
        InvoiceBatch.objects.filter(is_deleted=False)
        .annotate(_pending=Exists(pending))
        .filter(_pending=True)
        .values_list("company_id", "uuid")[:500]
    )
    for company_id, batch_uuid in batches:
        process_invoice_batch.apply_async(args=[company_id, str(batch_uuid)])
