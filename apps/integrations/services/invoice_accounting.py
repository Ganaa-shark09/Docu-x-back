from hashlib import sha256

from django.conf import settings
from django.db import transaction
from django.utils.module_loading import import_string
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.documents.services import DocumentPermissionService
from apps.workflows.models import InvoiceAccountingHandoff, InvoiceRecord
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services.workflow_engine_service import WorkflowConflict
from apps.workflows.services.workflow_events import workflow_event


class InvoiceAccountingService:
    """Explicit invoice posting boundary. No connector is configured by default."""

    @staticmethod
    def connector():
        path = getattr(settings, "DOCUX_INVOICE_ACCOUNTING_CONNECTOR", "")
        if not path:
            return None
        connector = import_string(path)()
        if not all(
            callable(getattr(connector, name, None)) for name in ("post", "lookup")
        ):
            raise ValidationError("Configured accounting connector is invalid.")
        return connector

    @staticmethod
    def provider_idempotency_key(invoice, client_key):
        """Scope new provider keys to a single invoice within one company.

        Existing handoffs retain their stored (legacy) provider keys so an
        uncertain historical post can still be reconciled by the ERP.
        """
        identity = f"{invoice.company_id}:{invoice.uuid}:{client_key}"
        return "docux:v2:" + sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def valid_external_reference(reference):
        """Accept only bounded, visible, canonical ERP posting identifiers.

        A posted state without a trustworthy reference remains unconfirmed.
        Never write provider-supplied control characters into an invoice record.
        """
        return (
            isinstance(reference, str)
            and 1 <= len(reference) <= 255
            and reference == reference.strip()
            and reference.isprintable()
        )

    @staticmethod
    def authorize(user, invoice):
        run = invoice.workflow_run
        if not WorkflowPermissionService.can(user, run, "administer"):
            raise PermissionDenied(
                "Workflow administration is required for accounting posting."
            )
        DocumentPermissionService.require_action(user, invoice.document, "export")
        if run.status not in {"completed", "approved"}:
            raise ValidationError("Only a finalized invoice can be posted.")

    @classmethod
    def status(cls, user, invoice):
        if not WorkflowPermissionService.content_allowed(user, invoice.workflow_run):
            raise PermissionDenied("Invoice content access is required.")
        handoff = invoice.accounting_handoffs.filter(is_deleted=False).first()
        ready = (
            invoice.workflow_run.status in {"completed", "approved"}
            and invoice.outputs.filter(is_deleted=False).exists()
        )
        return {
            "status": handoff.status
            if handoff
            else "not_ready"
            if not ready
            else "ready_to_post"
            if cls.connector()
            else "not_connected",
            "external_reference": handoff.external_reference if handoff else "",
            "failure_code": handoff.failure_code if handoff else "",
            "handoff_uuid": str(handoff.uuid) if handoff else None,
        }

    @classmethod
    @transaction.atomic
    def request_post(cls, user, invoice, idempotency_key):
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ValidationError("A bounded idempotency key is required.")
        invoice = (
            InvoiceRecord.objects.select_for_update()
            .select_related("document", "workflow_run")
            .get(pk=invoice.pk)
        )
        cls.authorize(user, invoice)
        connector = cls.connector()
        if not connector:
            raise ValidationError(
                "Accounting connector is not configured. Use the authorized export."
            )
        if invoice.document.sensitivity_label not in getattr(
            connector, "allowed_sensitivities", ()
        ):
            raise PermissionDenied(
                "Connector policy does not permit this document sensitivity."
            )
        output = invoice.outputs.filter(is_deleted=False).first()
        if not output:
            raise ValidationError("A finalized invoice output is required.")
        # Accept the old raw key for previously created handoffs. New handoffs
        # use a deterministic invoice-scoped provider key, avoiding accidental
        # cross-invoice deduplication by an ERP that scopes keys per company.
        provider_key = cls.provider_idempotency_key(invoice, idempotency_key)
        existing = invoice.accounting_handoffs.filter(
            idempotency_key__in=(idempotency_key, provider_key)
        ).first()
        if existing:
            # A soft-deleted handoff is not a valid replay target, but its
            # database uniqueness key still exists. Fail closed rather than
            # returning an inaccessible (possibly sensitive) old result.
            if existing.is_deleted:
                raise WorkflowConflict(
                    "The original accounting handoff is unavailable."
                )
            if existing.output_id != output.pk:
                raise WorkflowConflict()
            return existing, False
        if invoice.accounting_handoffs.filter(
            status__in=["pending", "posted", "uncertain"]
        ).exists():
            raise WorkflowConflict(
                "An accounting handoff already exists. Reconcile it before posting again."
            )
        handoff = InvoiceAccountingHandoff.objects.create(
            invoice=invoice,
            output=output,
            requested_by=user,
            idempotency_key=provider_key,
        )
        workflow_event(
            invoice.workflow_run,
            user,
            "invoice_accounting_requested",
            handoff_uuid=str(handoff.uuid),
        )
        cls.enqueue(handoff)
        return handoff, True

    @staticmethod
    def enqueue(handoff):
        from apps.integrations.tasks.invoice_accounting_tasks import post_invoice

        transaction.on_commit(
            lambda: post_invoice.delay(handoff.invoice.company_id, str(handoff.uuid)),
            robust=True,
        )

    @classmethod
    @transaction.atomic
    def process(cls, company_id, handoff_uuid):
        handoff = (
            InvoiceAccountingHandoff.objects.select_for_update(of=("self",))
            .filter(
                invoice__company_id=company_id,
                uuid=handoff_uuid,
                is_deleted=False,
            )
            .select_related(
                "invoice__document", "invoice__workflow_run", "output", "requested_by"
            )
            .first()
        )
        if not handoff or handoff.status != "pending":
            return False
        try:
            cls.authorize(handoff.requested_by, handoff.invoice)
        except (PermissionDenied, ValidationError):
            handoff.status = "rejected"
            handoff.failure_code = "authorization_or_state_changed"
            handoff.save(update_fields=["status", "failure_code", "updated_at"])
            workflow_event(
                handoff.invoice.workflow_run,
                handoff.requested_by,
                "invoice_accounting_rejected",
                reason="authorization_or_state_changed",
            )
            return False
        connector = cls.connector()
        if not connector or handoff.invoice.document.sensitivity_label not in getattr(
            connector, "allowed_sensitivities", ()
        ):
            handoff.status = "rejected"
            handoff.failure_code = "connector_unavailable"
            handoff.save(update_fields=["status", "failure_code", "updated_at"])
            workflow_event(
                handoff.invoice.workflow_run,
                handoff.requested_by,
                "invoice_accounting_rejected",
                reason="connector_unavailable",
            )
            return False
        try:
            result = connector.post(
                company_id=company_id,
                payload=handoff.output.data,
                idempotency_key=handoff.idempotency_key,
            )
        except Exception:
            # A timeout can mean the external system accepted the invoice.
            # Never automatically repost; explicit reconciliation must follow.
            result = {"status": "unknown"}
        if not isinstance(result, dict) or result.get("status") not in {
            "posted",
            "rejected",
            "unknown",
        }:
            result = {"status": "unknown"}
        reference = result.get("external_reference")
        if result["status"] == "posted" and not cls.valid_external_reference(reference):
            result = {"status": "unknown"}
        handoff.status = {
            "posted": "posted",
            "rejected": "rejected",
            "unknown": "uncertain",
        }[result["status"]]
        handoff.external_reference = reference if handoff.status == "posted" else ""
        handoff.failure_code = (
            ""
            if handoff.status == "posted"
            else "provider_rejected"
            if handoff.status == "rejected"
            else "confirmation_required"
        )
        handoff.save(
            update_fields=["status", "external_reference", "failure_code", "updated_at"]
        )
        workflow_event(
            handoff.invoice.workflow_run,
            handoff.requested_by,
            "invoice_accounting_result",
            status=handoff.status,
        )
        return handoff.status == "posted"

    @classmethod
    @transaction.atomic
    def reconcile(cls, user, invoice):
        invoice = (
            InvoiceRecord.objects.select_for_update()
            .select_related("document", "workflow_run")
            .get(pk=invoice.pk)
        )
        cls.authorize(user, invoice)
        handoff = (
            invoice.accounting_handoffs.select_for_update()
            .filter(status="uncertain", is_deleted=False)
            .first()
        )
        if not handoff:
            raise ValidationError(
                "There is no uncertain accounting handoff to reconcile."
            )
        connector = cls.connector()
        if not connector:
            raise ValidationError(
                "Accounting connector is unavailable. The posting remains unconfirmed."
            )
        try:
            result = connector.lookup(
                company_id=invoice.company_id, idempotency_key=handoff.idempotency_key
            )
        except Exception:
            result = {"status": "unknown"}
        if not isinstance(result, dict) or result.get("status") not in {
            "posted",
            "absent",
            "unknown",
        }:
            result = {"status": "unknown"}
        if result["status"] == "posted":
            reference = result.get("external_reference")
            if cls.valid_external_reference(reference):
                handoff.status = "posted"
                handoff.external_reference = reference
                handoff.failure_code = ""
                handoff.save(
                    update_fields=[
                        "status",
                        "external_reference",
                        "failure_code",
                        "updated_at",
                    ]
                )
                workflow_event(
                    invoice.workflow_run,
                    user,
                    "invoice_accounting_reconciled",
                    status="posted",
                )
        elif result["status"] == "absent":
            handoff.status = "pending"
            handoff.failure_code = ""
            handoff.save(update_fields=["status", "failure_code", "updated_at"])
            workflow_event(
                invoice.workflow_run,
                user,
                "invoice_accounting_reconciled",
                status="pending",
            )
            cls.enqueue(handoff)
        return handoff
