import json
import re
from decimal import Decimal
from types import SimpleNamespace

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workflows.models import ExtractionResultField, ExtractionRun
from apps.workflows.services.invoice_schema import (
    FIELDS,
    DATE_FIELDS,
    MONEY_FIELDS,
    LINE_NUMBERS,
    LINE_TEXT,
    InvoicePolicy,
)
from apps.workflows.services.structured_extraction_service import (
    StructuredExtractionService,
)
from apps.workflows.services.workflow_ai_service import WorkflowAIService


ALIASES = {
    "invoice_number": ["invoice number", "invoice no", "invoice #"],
    "vendor_name": ["vendor name", "vendor", "supplier"],
    "vendor_identifier": ["vendor identifier", "vendor id", "supplier id"],
    "vendor_tax_id": ["vendor tax id", "tax id", "gstin", "vat id"],
    "purchase_order_number": ["purchase order number", "po number", "po no"],
    "grand_total": ["grand total", "invoice total", "total amount"],
    "customer_reference": ["customer reference", "account reference"],
}


def field_specs():
    result = []
    for i, key in enumerate(FIELDS):
        aliases = ALIASES.get(key, [key.replace("_", " ")])
        expression = "|".join(re.escape(a) for a in aliases)
        result.append(
            {
                "field_key": key,
                "field_type": "date"
                if key in DATE_FIELDS
                else "amount"
                if key in MONEY_FIELDS
                else "json"
                if key == "line_items"
                else "text",
                "label": key.replace("_", " ").title(),
                "regex_pattern": rf"^[ \t]*(?:{expression})[ \t]*[:=][ \t]*([^\r\n]+)",
                "is_required": key in InvoicePolicy.DEFAULTS["required_fields"],
                "extraction_hint": "",
                "default_value": "",
                "order": i,
            }
        )
    return result


def evidence(document, text, start, end, method):
    if not 0 <= start < end <= len(text):
        raise ValidationError("Invalid invoice evidence offsets.")
    pages = list(re.finditer(r"^\[Page (\d+)\]$", text[:start], re.MULTILINE))
    page = int(pages[-1].group(1)) if pages else None
    extraction = document.text_extraction
    if page is not None and (page < 1 or page > extraction.page_count):
        raise ValidationError("Invalid invoice source page.")
    if page is None and extraction.page_count == 1:
        page = 1
    return {
        "document_uuid": str(document.uuid),
        "page": page,
        "start": start,
        "end": end,
        "preview": text[start:end][:500],
        "method": method,
        "bounding_box": None,
        "governance_revision": document.governance_revision,
    }


class InvoiceExtractionService:
    @classmethod
    def extract(cls, invoice, step_run, policy):
        document, user = invoice.document, invoice.workflow_run.initiated_by
        user.refresh_from_db()
        document.refresh_from_db()
        DocumentPermissionService.require_action(user, document, "extract")
        if FieldPermissionService.has_restrictions(
            user, document, "view"
        ) or FieldPermissionService.has_restrictions(user, document, "ai"):
            raise PermissionDenied(
                "Invoice extraction is denied by current field policies."
            )
        extraction = invoice.extraction_run
        if extraction and extraction.status in {"success", "review_required"}:
            return
        if not extraction:
            from apps.workflows.models import ExtractionTemplate

            template = ExtractionTemplate.objects.get(
                is_system=True,
                company=None,
                name="Canonical Invoice Schema v1",
                is_deleted=False,
            )
            extraction = ExtractionRun.objects.create(
                company=invoice.company,
                document=document,
                template=template,
                requested_by=user,
            )
            invoice.extraction_run = extraction
            invoice.save(update_fields=["extraction_run", "updated_at"])
            step_run.extraction = extraction
            step_run.save(update_fields=["extraction", "updated_at"])
        text_extraction = getattr(document, "text_extraction", None)
        text = text_extraction.extracted_text if text_extraction else ""
        if len(text.encode()) > 512000:
            raise ValidationError("Invoice text exceeds the supported extraction size.")
        extraction.mark_running()
        ai_values = None
        ai_evidence = []
        if policy["use_ai"] and text.strip():
            output, ai_evidence, execution = WorkflowAIService.execute_structured(
                invoice.workflow_run,
                step_run,
                instruction=cls.ai_instruction(),
                validator=cls.validate_ai_output,
                top_k=20,
            )
            ai_values = output["fields"]
            step_run.execution_metadata = execution
            step_run.save(update_fields=["execution_metadata", "updated_at"])
        for spec in step_run.step.execution_snapshot["fields"]:
            key = spec["field_key"]
            value, score, proof, metadata = "", None, [], {"method": "not_found"}
            if ai_values is not None and key in ai_values:
                item = ai_values[key]
                value = item["raw_value"]
                score = Decimal(str(item["confidence"])) * 100
                proof = [e for e in ai_evidence if e["chunk_uuid"] in item["evidence"]]
                metadata = {"method": "model", "confidence_basis": "model_reported"}
            elif key == "line_items":
                value, proof = cls.extract_lines(document, text) if text else (None, [])
                if value is not None:
                    score = Decimal(92)
                    metadata = {
                        "method": "table_header",
                        "confidence_basis": "explicit_header_cells",
                    }
            elif text:
                # Reuse the existing template extractor with anchored label specs.
                item = StructuredExtractionService.extract_field(
                    text, SimpleNamespace(**spec)
                )
                match = re.search(
                    spec["regex_pattern"], text, re.IGNORECASE | re.MULTILINE
                )
                if match:
                    value = item["value"]
                    score = Decimal(str(item["confidence"]))
                    proof = [
                        evidence(
                            document, text, match.start(1), match.end(1), "label_regex"
                        )
                    ]
                    metadata = {
                        "method": "label_regex",
                        "confidence_basis": "explicit_label_match",
                    }
            if (
                score is not None
                and text_extraction
                and text_extraction.extraction_method == "ocr"
            ):
                score = min(score, text_extraction.confidence_score)
                metadata["ocr_confidence_cap"] = str(text_extraction.confidence_score)
            metadata["evidence"] = proof
            metadata["extracted_confidence"] = (
                str(score / 100) if score is not None else None
            )
            raw = (
                json.dumps(value, allow_nan=False)
                if key == "line_items" and value is not None
                else value or ""
            )
            ExtractionResultField.objects.create(
                extraction_run=extraction,
                field_key=key,
                label=spec["label"],
                extracted_value=raw,
                confidence_score=score or 0,
                source_text=proof[0].get("preview", "") if proof else "",
                source_metadata=metadata,
                is_required=key in policy["required_fields"],
                is_missing=raw == "",
            )
        # Recheck after the model call and before the atomic field set commits.
        user.refresh_from_db()
        document.refresh_from_db()
        DocumentPermissionService.require_action(user, document, "extract")
        if FieldPermissionService.has_restrictions(
            user, document, "ai"
        ) or FieldPermissionService.has_restrictions(user, document, "view"):
            raise PermissionDenied("Invoice policies changed during extraction.")
        extraction.raw_output = {
            f.field_key: {
                "value": f.extracted_value or None,
                "confidence": f.source_metadata["extracted_confidence"],
            }
            for f in extraction.result_fields.all()
        }
        extraction.status = "review_required"
        extraction.completed_at = timezone.now()
        extraction.save(
            update_fields=["raw_output", "status", "completed_at", "updated_at"]
        )

    @staticmethod
    def ai_instruction():
        return (
            "Extract invoice fields only from the supplied evidence. Return a JSON object "
            "with exactly one key fields, mapping allowed field names to objects with "
            "raw_value (source string, or line_items array of string/null cells), "
            "confidence (0..1), evidence (nonempty authorized chunk UUID array). "
            "Omit unavailable fields; never infer values or evidence. Allowed fields: "
            + ", ".join(FIELDS)
            + ". Line item cells: "
            + ", ".join((*LINE_TEXT, *LINE_NUMBERS))
            + ". Every non-null cell of each line item must appear together "
            "within at least one cited evidence chunk. Omit empty rows."
        )

    @staticmethod
    def validate_ai_output(answer, chunks):
        from apps.workflows.services.workflow_definition_service import bounded_json

        if not isinstance(answer, str) or len(answer.encode()) > 32768:
            raise ValidationError("Invoice model response is not structured input.")
        try:
            output = json.loads(answer)
        except (ValueError, RecursionError):
            raise ValidationError("Invoice model response must be JSON.")
        bounded_json(output)
        if (
            not isinstance(output, dict)
            or set(output) != {"fields"}
            or not isinstance(output["fields"], dict)
            or set(output["fields"]) - set(FIELDS)
        ):
            raise ValidationError("Invoice model response does not match the schema.")
        sources = {str(c["chunk_uuid"]): c for c in chunks}
        for key, item in output["fields"].items():
            if not isinstance(item, dict) or set(item) != {
                "raw_value",
                "confidence",
                "evidence",
            }:
                raise ValidationError("Invalid invoice model field schema.")
            if (
                type(item["confidence"]) not in {int, float}
                or not 0 <= item["confidence"] <= 1
            ):
                raise ValidationError("Invalid invoice model confidence.")
            refs = item["evidence"]
            if (
                not isinstance(refs, list)
                or not 1 <= len(refs) <= 20
                or any(not isinstance(r, str) or r not in sources for r in refs)
            ):
                raise ValidationError("Invoice model evidence is unavailable.")
            raw = item["raw_value"]
            evidence_texts = [sources[ref]["content"] for ref in refs]
            if key == "line_items":
                if (
                    not isinstance(raw, list)
                    or not raw
                    or len(raw) > 200
                    or any(
                        not isinstance(row, dict)
                        or not row
                        or set(row) - set((*LINE_TEXT, *LINE_NUMBERS))
                        for row in raw
                    )
                ):
                    raise ValidationError("Invalid invoice line items.")
                for row in raw:
                    cells = [value for value in row.values() if value is not None]
                    # Each claimed row needs a coherent source. Combining
                    # unrelated chunks must not fabricate a new line item.
                    if (
                        not cells
                        or any(
                            not isinstance(value, str) or not 1 <= len(value) <= 4000
                            for value in cells
                        )
                        or not any(
                            all(value in content for value in cells)
                            for content in evidence_texts
                        )
                    ):
                        raise ValidationError(
                            "Invoice line item values must share source evidence."
                        )
            elif (
                not isinstance(raw, str)
                or not 1 <= len(raw) <= 4000
                or not any(raw in content for content in evidence_texts)
            ):
                raise ValidationError(
                    "Invoice values must occur in their source evidence."
                )
        return output

    @staticmethod
    def extract_lines(document, text):
        aliases = {
            "qty": "quantity",
            "item_code": "sku",
            "amount": "line_total",
            "price": "unit_price",
        }
        offsets = list(re.finditer(r"[^\r\n]+", text))
        for position, line in enumerate(offsets):
            separator = (
                "|" if "|" in line.group() else "\t" if "\t" in line.group() else None
            )
            if not separator:
                continue
            header = [
                x.strip().lower().replace(" ", "_")
                for x in line.group().split(separator)
            ]
            header = [aliases.get(h, h) for h in header]
            if (
                "description" not in header
                or "line_total" not in header
                or len(set(header)) != len(header)
            ):
                continue
            rows, proof = [], []
            for row in offsets[position + 1 : position + 202]:
                if separator not in row.group():
                    break
                values = [v.strip() for v in row.group().split(separator)]
                if len(values) != len(header):
                    raise ValidationError(
                        "Invoice table row does not match its header."
                    )
                if len(rows) >= 200:
                    raise ValidationError("Invoice line item limit exceeded.")
                rows.append(
                    {
                        k: v or None
                        for k, v in zip(header, values)
                        if k in (*LINE_TEXT, *LINE_NUMBERS)
                    }
                )
                proof.append(
                    evidence(document, text, row.start(), row.end(), "table_header")
                )
            if rows:
                return rows, proof
        return None, []
