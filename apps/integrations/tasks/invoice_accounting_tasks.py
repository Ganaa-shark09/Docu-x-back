from celery import shared_task
from datetime import timedelta
from django.utils import timezone

from apps.integrations.services.invoice_accounting import InvoiceAccountingService
from apps.workflows.models import InvoiceAccountingHandoff


@shared_task(acks_late=True, reject_on_worker_lost=True)
def post_invoice(company_id, handoff_uuid):
    return InvoiceAccountingService.process(company_id, handoff_uuid)


@shared_task
def dispatch_pending_invoice_handoffs():
    for company_id, handoff_uuid in (
        InvoiceAccountingHandoff.objects.filter(
            status="pending",
            is_deleted=False,
            updated_at__lte=timezone.now() - timedelta(seconds=30),
        )
        .order_by("updated_at")
        .values_list("invoice__company_id", "uuid")[:500]
    ):
        post_invoice.delay(company_id, str(handoff_uuid))
