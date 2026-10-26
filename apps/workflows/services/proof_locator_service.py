"""Validate location against the resolved version; never trust generated quotes."""

import re

from rest_framework.exceptions import ValidationError

from apps.vector_search.models import DocumentChunk
from apps.workflows.models import FinanceTransaction
from apps.workflows.services.finance_audit_service import digest
from .proof_source_service import output_path, reference


def invalid(code="locator_invalid"):
    raise ValidationError({"code": code})


class ProofLocatorService:
    @staticmethod
    def validate(resolver, resolved, locator_type, locator, precision, excerpt=None):
        if not isinstance(locator, dict):
            invalid()
        if precision not in {
            "exact",
            "approximate",
            "structured_record",
            "derived",
            "aggregate",
        }:
            invalid()
        kind = resolved["pin"]["kind"]
        if locator_type == "output_path":
            if set(locator) != {"path"} or kind == "document":
                invalid()
            value = output_path(resolved["data"], locator["path"])
            if precision not in {"structured_record", "derived", "aggregate"}:
                invalid()
            if excerpt is not None and excerpt != str(value):
                invalid("excerpt_mismatch")
            return {"path": locator["path"], "value_digest": digest(value)}
        if locator_type in {"dataset_row", "transaction"}:
            if (
                kind not in {"finance_dataset", "finance_transaction"}
                or set(locator) != {"transaction_uuid"}
                or precision != "structured_record"
            ):
                invalid()
            obj = resolved["object"]
            dataset = obj if kind == "finance_dataset" else obj.dataset
            row = FinanceTransaction.objects.filter(
                uuid=reference(locator["transaction_uuid"]),
                dataset=dataset,
                is_deleted=False,
            ).first()
            if (
                not row
                or (kind == "finance_transaction" and row.pk != obj.pk)
                or excerpt is not None
            ):
                invalid()
            return {
                "transaction_uuid": str(row.uuid),
                "row": row.row,
                "row_digest": digest(row.data),
            }
        if locator_type == "source":
            if locator or precision != "approximate" or excerpt is not None:
                invalid()
            return {}
        allowed = {"document_uuid", "start", "end", "page", "chunk_uuid"}
        if set(locator) - allowed or "document_uuid" not in locator:
            invalid()
        doc = next(
            (
                d
                for d in resolved["documents"]
                if str(d.uuid) == reference(locator["document_uuid"])
            ),
            None,
        )
        if not doc:
            invalid()
        pin, text = resolver.document(doc)
        clean = {"document_uuid": str(doc.uuid)}
        page = locator.get("page")
        if page is not None:
            if type(page) is not int or not 1 <= page <= pin["page_count"]:
                invalid()
            clean["page"] = page
        if locator_type == "text_offsets":
            start, end = locator.get("start"), locator.get("end")
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(text)
                or end - start > 8000
                or precision != "exact"
                or "chunk_uuid" in locator
            ):
                invalid()
            if excerpt is not None and excerpt != text[start:end]:
                invalid("excerpt_mismatch")
            if page is not None:
                markers = list(re.finditer(r"(?m)^\[Page (\d+)\]$", text[:start]))
                actual = (
                    int(markers[-1][1])
                    if markers
                    else 1
                    if pin["page_count"] == 1
                    else None
                )
                if actual != page or re.search(r"(?m)^\[Page \d+\]$", text[start:end]):
                    invalid()
            clean.update(start=start, end=end)
        elif locator_type == "chunk":
            if (
                set(locator) - {"document_uuid", "chunk_uuid", "page"}
                or precision != "approximate"
            ):
                invalid()
            chunk = DocumentChunk.objects.filter(
                uuid=reference(locator.get("chunk_uuid")),
                document=doc,
                company_id=doc.company_id,
                is_deleted=False,
            ).first()
            if not chunk or (page is not None and chunk.page_number != page):
                invalid()
            if not chunk.content.strip() or chunk.content not in text:
                invalid("source_changed")
            metadata = chunk.metadata or {}
            if "start_char" in metadata or "end_char" in metadata:
                start, end = metadata.get("start_char"), metadata.get("end_char")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not 0 <= start < end <= len(text.strip())
                    or text.strip()[start:end].strip() != chunk.content
                ):
                    invalid("source_changed")
            if excerpt is not None and excerpt not in chunk.content:
                invalid("excerpt_mismatch")
            clean.update(chunk_uuid=str(chunk.uuid), chunk_digest=digest(chunk.content))
        elif locator_type == "page":
            if (
                set(locator) != {"document_uuid", "page"}
                or precision != "approximate"
                or excerpt is not None
            ):
                invalid()
        else:
            invalid()
        return clean

    @classmethod
    def revalidate(cls, resolver, resolved, evidence):
        # Digests and server-derived row metadata are not client locator fields.
        locator = {
            k: v
            for k, v in evidence.locator.items()
            if k not in {"chunk_digest", "row_digest", "value_digest", "row"}
        }
        current = cls.validate(
            resolver, resolved, evidence.locator_type, locator, evidence.precision
        )
        if current != evidence.locator:
            invalid("source_changed")
