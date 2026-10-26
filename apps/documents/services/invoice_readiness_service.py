"""Server-owned, non-LLM invoice preflight. No raw source text leaves this service.

Readiness is computed from current document metadata/text, so there is no stale
second database state and no model/provider or external data transfer.
"""

import re

from rest_framework.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.documents.models import Document
from apps.documents.services.document_permission_service import (
    DocumentPermissionService,
)
from apps.documents.services.field_permission_service import FieldPermissionService


class InvoiceReadinessService:
    SIGNALS = {
        "invoice_number": (r"\binvoice\s*(?:number|no\.?|#|id)\b",),
        "invoice_date": (r"\binvoice\s*date\b", r"\bdate\s*of\s*invoice\b"),
        "vendor": (r"\b(?:seller|supplier|vendor|from)(?:\s+name)?\s*[:\-]",),
        "currency": (r"\b(?:currency\s*[:\-]|INR|USD|EUR|GBP)\b",),
        "subtotal": (r"\bsub\s*total\b",),
        "grand_total": (r"\b(?:grand\s*total|total\s*(?:amount|due|payable))\b",),
    }
    NON_INVOICE_SIGNALS = (
        r"\bthis agreement\b",
        r"\bgoverning law\b",
        r"\btermination clause\b",
        r"\bconfidentiality agreement\b",
        r"\bemployment contract\b",
    )

    @classmethod
    def assess(cls, document, *, user=None):
        """Safe status and signal presence only; never expose inferred values."""
        ai_ready = bool(document.is_ai_ready and document.is_indexed)
        result = {
            "status": "processing",
            "can_start_invoice_workflow": False,
            "internal_ai_indexed": ai_ready,
            "message": "Document processing is in progress.",
            "checks": [],
            "missing_signals": [],
        }
        if user is not None and FieldPermissionService.has_restrictions(
            user, document, "view"
        ):
            return {
                **result,
                "status": "restricted",
                "message": "Document field permissions restrict preflight inspection.",
            }
        if document.scope != Document.Scope.INTERNAL or document.is_deleted:
            return {
                **result,
                "status": "blocked",
                "message": "Only accessible internal documents can enter invoice workflows.",
            }
        expires_at = getattr(document, "retention_expires_at", None)
        if expires_at is not None and expires_at <= timezone.now():
            return {
                **result,
                "status": "blocked",
                "message": "Document retention has expired. Processing is unavailable.",
            }
        if document.document_type != Document.DocumentType.INVOICE:
            return {
                **result,
                "status": "blocked",
                "message": "Document type is not Invoice. Verify classification in Documents.",
            }
        if document.status in {Document.Status.FAILED, Document.Status.ARCHIVED}:
            return {
                **result,
                "status": "blocked",
                "message": "Restore or reprocess this document before invoice processing.",
            }
        if document.status != Document.Status.READY:
            return result

        extracted = getattr(document, "text_extraction", None)
        text = (getattr(extracted, "extracted_text", "") or "")[:50000]
        if not text.strip():
            return {
                **result,
                "status": "needs_review",
                "can_start_invoice_workflow": True,
                "message": "No searchable source text is available. Check OCR and source evidence before approval.",
            }

        checks = [
            {"key": name, "present": any(re.search(p, text, re.I) for p in patterns)}
            for name, patterns in cls.SIGNALS.items()
        ]
        missing = [check["key"] for check in checks if not check["present"]]
        contradicting = sum(
            bool(re.search(p, text, re.I)) for p in cls.NON_INVOICE_SIGNALS
        )
        if contradicting >= 2 and not any(check["present"] for check in checks):
            return {
                **result,
                "status": "blocked",
                "checks": checks,
                "message": "Document content resembles another document type. Verify classification before processing.",
            }
        if missing:
            return {
                **result,
                "status": "needs_review",
                "can_start_invoice_workflow": True,
                "checks": checks,
                "missing_signals": missing,
                "message": "Invoice content needs verification. Missing labels are not proof that values are absent.",
            }
        return {
            **result,
            "status": "ready",
            "can_start_invoice_workflow": True,
            "checks": checks,
            "message": "Invoice-like source labels were found. Values still require source-backed review.",
        }

    @classmethod
    def require_batch_source(cls, user, document):
        if not DocumentPermissionService.can_action(user, document, "edit"):
            raise PermissionDenied("Document edit permission is required.")
        if FieldPermissionService.has_restrictions(user, document, "view"):
            raise PermissionDenied(
                "Restricted document fields prevent invoice processing by this account."
            )
        assessment = cls.assess(document)
        if assessment["status"] == "blocked":
            raise ValidationError({"documents": [assessment["message"]]})
        # 'processing' and 'needs_review' are not silent hard failures: queued
        # documents may finish asynchronously and imperfect invoices enter review.
        return assessment
