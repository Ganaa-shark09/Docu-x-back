import re
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.workflows.models import ExtractionResultField, ExtractionRun


class StructuredExtractionService:
    @classmethod
    @transaction.atomic
    def run_extraction(cls, extraction_run):
        extraction_run.mark_running()

        document = extraction_run.document
        extraction = getattr(document, "text_extraction", None)

        if not extraction or not extraction.extracted_text.strip():
            extraction_run.mark_failed("No extracted text available for this document.")
            return extraction_run

        text = extraction.extracted_text
        fields = extraction_run.template.fields.filter(is_deleted=False).order_by(
            "order"
        )

        result_payload = {}
        confidence_values = []

        ExtractionResultField.objects.filter(extraction_run=extraction_run).delete()

        for template_field in fields:
            result = cls.extract_field(text=text, template_field=template_field)

            extracted_value = result["value"]
            confidence = result["confidence"]
            is_missing = not bool(str(extracted_value).strip())

            ExtractionResultField.objects.create(
                extraction_run=extraction_run,
                field_key=template_field.field_key,
                label=template_field.label,
                extracted_value=extracted_value,
                normalized_value=result.get("normalized_value", {}),
                confidence_score=confidence,
                source_text=result.get("source_text", ""),
                source_metadata=result.get("source_metadata", {}),
                is_required=template_field.is_required,
                is_missing=is_missing,
            )

            result_payload[template_field.field_key] = {
                "label": template_field.label,
                "value": extracted_value,
                "confidence": float(confidence),
                "is_missing": is_missing,
                "source_text": result.get("source_text", ""),
            }

            confidence_values.append(float(confidence))

        overall_confidence = (
            sum(confidence_values) / len(confidence_values) if confidence_values else 0
        )

        required_missing = any(item["is_missing"] for item in result_payload.values())

        extraction_run.raw_output = result_payload
        extraction_run.overall_confidence = Decimal(str(round(overall_confidence, 2)))
        extraction_run.status = (
            ExtractionRun.Status.REVIEW_REQUIRED
            if required_missing or overall_confidence < 80
            else ExtractionRun.Status.SUCCESS
        )
        extraction_run.completed_at = timezone.now()
        extraction_run.error_message = ""
        extraction_run.save(
            update_fields=[
                "raw_output",
                "overall_confidence",
                "status",
                "completed_at",
                "error_message",
                "updated_at",
            ]
        )

        return extraction_run

    @classmethod
    def extract_field(cls, text, template_field):
        if template_field.regex_pattern:
            match = re.search(
                template_field.regex_pattern, text, flags=re.IGNORECASE | re.MULTILINE
            )

            if match:
                value = match.group(1) if match.groups() else match.group(0)
                return {
                    "value": value.strip(),
                    "confidence": 92,
                    "source_text": match.group(0).strip(),
                    "source_metadata": {
                        "method": "regex_pattern",
                        "pattern": template_field.regex_pattern,
                    },
                }

        return cls.extract_by_label_heuristic(text, template_field)

    @classmethod
    def extract_by_label_heuristic(cls, text, template_field):
        possible_labels = cls.build_possible_labels(template_field)

        lines = [line.strip() for line in text.splitlines() if line.strip()]

        for line in lines:
            lower_line = line.lower()

            for label in possible_labels:
                if label in lower_line:
                    value = cls.extract_value_from_line(line, label)

                    if value:
                        return {
                            "value": value,
                            "confidence": 75,
                            "source_text": line,
                            "source_metadata": {
                                "method": "label_heuristic",
                                "matched_label": label,
                            },
                        }

        return {
            "value": template_field.default_value or "",
            "confidence": 20 if template_field.default_value else 0,
            "source_text": "",
            "source_metadata": {
                "method": "not_found",
            },
        }

    @staticmethod
    def build_possible_labels(template_field):
        labels = {
            template_field.field_key.replace("_", " ").lower(),
            template_field.label.lower(),
        }

        if template_field.extraction_hint:
            labels.add(template_field.extraction_hint.lower())

        return list(labels)

    @staticmethod
    def extract_value_from_line(line, matched_label):
        # Handles:
        # Invoice Number: INV-001
        # Amount Due - 15000
        # Payment Terms 30 days
        separators = [":", "-", "="]

        for separator in separators:
            if separator in line:
                parts = line.split(separator, 1)
                if len(parts) == 2:
                    return parts[1].strip()

        lower_line = line.lower()
        index = lower_line.find(matched_label)

        if index >= 0:
            return line[index + len(matched_label) :].strip(" :-=")

        return ""
