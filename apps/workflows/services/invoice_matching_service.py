from decimal import Decimal, InvalidOperation

from django.db.models import Case, IntegerField, Q, Value, When
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.tenants.models import Company
from apps.workflows.models import InvoiceRecord, InvoiceVendor, WorkflowRun
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services.invoice_schema import precision


def accessible_invoice_documents(user):
    return (
        DocumentPermissionService.get_accessible_document_queryset(
            user, scope="internal"
        )
        .filter(
            Q(retention_expires_at__isnull=True)
            | Q(retention_expires_at__gt=timezone.now())
        )
        .exclude(pk__in=FieldPermissionService.denied_document_ids(user, "view"))
    )


def accessible_invoice_records(user):
    return InvoiceRecord.objects.filter(
        company_id=user.company_id,
        is_deleted=False,
        document__in=accessible_invoice_documents(user),
        workflow_run__in=WorkflowPermissionService.visible_runs(
            user, WorkflowRun.objects.all()
        ),
    )


class InvoiceMatchingService:
    purchase_order_adapters = {}

    @staticmethod
    def duplicates(invoice, user):
        # Serialize the decision against other invoice duplicate checks in this tenant.
        Company.objects.select_for_update().get(pk=invoice.company_id)
        # Rank strong, independently verifiable signals above number/vendor
        # matches, so a busy tenant cannot hide an exact duplicate behind the
        # first fifty older probable matches. Candidates remain review-only.
        exact = Q(pk__in=[])
        has_exact_signal = False
        if invoice.document.checksum:
            exact |= Q(document__checksum=invoice.document.checksum)
            has_exact_signal = True
        if invoice.fingerprint:
            exact |= Q(fingerprint=invoice.fingerprint)
            has_exact_signal = True

        condition = exact
        if invoice.invoice_number and invoice.vendor_key:
            condition |= Q(
                invoice_number=invoice.invoice_number, vendor_key=invoice.vendor_key
            )
        if not has_exact_signal and not (invoice.invoice_number and invoice.vendor_key):
            return []

        rows = (
            accessible_invoice_records(user)
            .filter(condition, company_id=invoice.company_id, is_deleted=False)
            .exclude(pk=invoice.pk)
            .select_related("document")
        )
        if has_exact_signal:
            rows = rows.annotate(
                _duplicate_rank=Case(
                    When(exact, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            ).order_by("-_duplicate_rank", "-created_at", "-pk")
        else:
            rows = rows.order_by("-created_at", "-pk")
        rows = rows[:50]
        matches = []
        for row in rows:
            signals = []
            if (
                invoice.document.checksum
                and row.document.checksum == invoice.document.checksum
            ):
                signals.append("document_checksum")
            if invoice.fingerprint and row.fingerprint == invoice.fingerprint:
                signals.append("normalized_fingerprint")
            if invoice.invoice_number and row.invoice_number == invoice.invoice_number:
                signals.append("invoice_number")
            if invoice.vendor_key and row.vendor_key == invoice.vendor_key:
                signals.append("vendor_identity")
            matches.append(
                {
                    "invoice_uuid": str(row.uuid),
                    "document_uuid": str(row.document.uuid),
                    "match_strength": "exact"
                    if any(
                        s in signals
                        for s in ("document_checksum", "normalized_fingerprint")
                    )
                    else "probable",
                    "signals": signals,
                    "status": "candidate",
                }
            )
        return matches

    @staticmethod
    def vendor(invoice):
        values = invoice.normalized
        query = Q(pk__in=[])
        strong = False
        if values["vendor_identifier"]:
            query |= Q(identifier=values["vendor_identifier"])
            strong = True
        if values["vendor_tax_id"]:
            query |= Q(tax_id=values["vendor_tax_id"])
            strong = True
        if not strong and values["vendor_name"]:
            query |= Q(name__iexact=values["vendor_name"])
        candidates = list(
            InvoiceVendor.objects.filter(
                query,
                company_id=invoice.company_id,
                is_active=True,
                is_deleted=False,
            ).order_by("pk")[:21]
        )
        # Conflicting supplied identifiers are never silently accepted.
        compatible = [
            v
            for v in candidates
            if not (
                (
                    values["vendor_identifier"]
                    and v.identifier != values["vendor_identifier"]
                )
                or (values["vendor_tax_id"] and v.tax_id != values["vendor_tax_id"])
            )
        ]
        matched = (
            compatible[0] if len(compatible) == 1 and len(candidates) < 21 else None
        )
        return {
            "status": "matched"
            if matched
            else "ambiguous"
            if candidates
            else "not_matched",
            "vendor_uuid": str(matched.uuid) if matched else None,
            "match_type": "exact_identifier"
            if strong
            else "exact_name"
            if matched
            else None,
            "confidence": None,
            "candidates": [str(v.uuid) for v in candidates[:20]],
        }

    @classmethod
    def purchase_order(cls, invoice, user, policy):
        reference = invoice.normalized["purchase_order_number"]
        if not reference:
            return {"status": "not_applicable", "reference": None, "checks": []}
        adapter = cls.purchase_order_adapters.get(policy["po_adapter"])
        if adapter is None:
            return {
                "status": "integration_unavailable",
                "reference": reference,
                "checks": [],
            }
        result = adapter.match(
            company=invoice.company,
            user=user,
            invoice=invoice.normalized,
            vendor_match=invoice.matches.get("vendor", {}),
            source_document=invoice.document,
        )
        from apps.workflows.services.workflow_definition_service import bounded_json

        bounded_json(result)
        allowed = {
            "company_id",
            "status",
            "reference",
            "currency",
            "amount",
            "document_uuids",
        }
        if (
            not isinstance(result, dict)
            or set(result) - allowed
            or type(result.get("company_id")) is not int
            or result.get("company_id") != invoice.company_id
            or result.get("status")
            not in ("matched", "not_matched", "ambiguous", "integration_unavailable")
        ):
            raise ValidationError("Invalid purchase order adapter result.")
        refs = result.get("document_uuids", [])
        if (
            not isinstance(refs, list)
            or len(refs) > 20
            or any(not isinstance(x, str) for x in refs)
        ):
            raise ValidationError("Invalid purchase order source references.")
        try:
            count = accessible_invoice_documents(user).filter(uuid__in=refs).count()
        except (ValueError, TypeError, DjangoValidationError):
            raise ValidationError("Invalid purchase order source references.")
        if count != len(set(refs)):
            raise ValidationError("Purchase order references are not authorized.")
        if result.get("reference") is not None and (
            not isinstance(result["reference"], str) or result["reference"] != reference
        ):
            raise ValidationError(
                "Purchase order reference does not match its request."
            )
        checks = []
        if result["status"] == "matched":
            if result.get("reference") != reference:
                raise ValidationError(
                    "Matched purchase orders require the requested reference."
                )
            if result.get("currency") != invoice.normalized["currency"]:
                checks.append("currency_mismatch")
            try:
                if not isinstance(result.get("amount"), str):
                    raise ValueError
                amount = Decimal(result["amount"])
                if not amount.is_finite():
                    raise ValueError
                total = invoice.normalized["grand_total"]
                tolerance = (
                    Decimal(policy["tolerance"])
                    if policy["tolerance"] is not None
                    else Decimal(1).scaleb(-precision(invoice.normalized["currency"]))
                )
                if total is not None and abs(amount - Decimal(total)) > tolerance:
                    checks.append("po_amount_mismatch")
            except (InvalidOperation, ValueError):
                raise ValidationError("Invalid purchase order monetary amount.")
        return {"status": result["status"], "reference": reference, "checks": checks}
