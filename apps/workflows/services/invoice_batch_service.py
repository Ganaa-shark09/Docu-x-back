from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService
from apps.documents.services.invoice_readiness_service import InvoiceReadinessService
from apps.tenants.models import Company
from apps.workflows.models import InvoiceBatch, InvoiceBatchItem, WorkflowVersion
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
    fingerprint,
)


class InvoiceBatchService:
    @staticmethod
    def enqueue(batch):
        from apps.workflows.tasks.invoice_batch_tasks import process_invoice_batch

        transaction.on_commit(
            lambda: process_invoice_batch.apply_async(
                args=[batch.company_id, str(batch.uuid)], countdown=10
            ),
            robust=True,
        )

    @classmethod
    @transaction.atomic
    def start_waiting(cls, company_id, batch_uuid):
        batch = (
            InvoiceBatch.objects.select_for_update(of=("self",))
            .filter(
                company_id=company_id,
                uuid=batch_uuid,
                is_deleted=False,
            )
            .select_related("created_by", "version")
            .first()
        )
        if not batch:
            return False
        if not WorkflowPermissionService.active(batch.created_by):
            batch.items.filter(error_code="awaiting_document", run__isnull=True).update(
                error_code="actor_unavailable",
                error_message="The initiating account is no longer authorized. An administrator must review this batch.",
            )
            return False
        waiting = False
        for item in (
            InvoiceBatchItem.objects.select_for_update()
            .filter(
                batch=batch,
                error_code="awaiting_document",
                run__isnull=True,
                is_deleted=False,
            )
            .select_related("document")
            .order_by("position")[:20]
        ):
            document = item.document
            if document.status in {"failed", "archived"} or document.is_deleted:
                item.error_code = "source_unavailable"
                item.error_message = (
                    "Document processing failed or the source is unavailable."
                )
            elif document.status != "ready":
                if item.created_at > timezone.now() - timedelta(hours=24):
                    waiting = True
                    continue
                item.error_code = "source_timeout"
                item.error_message = "Document processing did not finish within 24 hours. Review and retry."
            else:
                try:
                    InvoiceReadinessService.require_batch_source(
                        batch.created_by, document
                    )
                    with transaction.atomic():
                        run, _ = WorkflowEngineService.start(
                            batch.created_by,
                            version_uuid=batch.version.uuid,
                            document_uuids=[str(document.uuid)],
                            idempotency_key=f"invoice-batch:{batch.uuid}:{item.position}",
                            metadata={"invoice_batch_uuid": str(batch.uuid)},
                        )
                        item.run = run
                except (PermissionDenied, ValidationError) as exc:
                    item.error_code = "start_rejected"
                    item.error_message = str(exc.detail)[:255]
                else:
                    item.error_code = item.error_message = ""
            item.save(
                update_fields=["run", "error_code", "error_message", "updated_at"]
            )
        return (
            waiting
            or batch.items.filter(
                error_code="awaiting_document",
                run__isnull=True,
                is_deleted=False,
            ).exists()
        )

    @classmethod
    @transaction.atomic
    def create(cls, user, *, name, version_uuid, document_uuids, idempotency_key):
        if not WorkflowPermissionService.active(user):
            raise PermissionDenied("An active company account is required.")
        if not 1 <= len(document_uuids) <= 500 or len(set(document_uuids)) != len(
            document_uuids
        ):
            raise ValidationError("Select 1–500 distinct invoice documents.")
        version = (
            WorkflowVersion.objects.select_related("definition")
            .filter(
                uuid=version_uuid,
                definition__company_id=user.company_id,
                definition__workflow_type="invoice",
                definition__is_active=True,
                definition__is_deleted=False,
                is_deleted=False,
            )
            .first()
        )
        if not version or not WorkflowPermissionService.department_allowed(
            user, version.definition.department_id
        ):
            raise PermissionDenied("Published invoice workflow is unavailable.")
        documents = list(
            Document.objects.filter(
                uuid__in=document_uuids,
                company_id=user.company_id,
                scope="internal",
                is_deleted=False,
            )
        )
        by_uuid = {str(doc.uuid): doc for doc in documents}
        if len(by_uuid) != len(document_uuids) or any(
            not DocumentPermissionService.can_action(user, doc, "edit")
            for doc in documents
        ):
            raise PermissionDenied(
                "Current document permission is required for every invoice."
            )
        for doc in documents:
            InvoiceReadinessService.require_batch_source(user, doc)
        request_hash = fingerprint(
            {
                "actor": user.pk,
                "name": name,
                "version": str(version.uuid),
                "documents": document_uuids,
            }
        )
        Company.objects.select_for_update().get(pk=user.company_id)
        existing = InvoiceBatch.objects.filter(
            company_id=user.company_id,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            if existing.request_fingerprint != request_hash:
                raise WorkflowConflict()
            return existing, False
        batch = InvoiceBatch.objects.create(
            company=user.company,
            created_by=user,
            version=version,
            name=name,
            idempotency_key=idempotency_key,
            request_fingerprint=request_hash,
        )
        for position, document_uuid in enumerate(document_uuids, 1):
            item = InvoiceBatchItem.objects.create(
                batch=batch,
                document=by_uuid[document_uuid],
                position=position,
            )
            if item.document.status in {"failed", "archived"}:
                item.error_code = "source_unavailable"
                item.error_message = (
                    "Document processing failed or the source is unavailable."
                )
                item.save(update_fields=["error_code", "error_message", "updated_at"])
                continue
            item.error_code = "awaiting_document"
            item.save(update_fields=["error_code", "updated_at"])
        if batch.items.filter(error_code="awaiting_document").exists():
            cls.enqueue(batch)
        return batch, True
