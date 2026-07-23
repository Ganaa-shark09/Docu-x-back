from django.core.management.base import BaseCommand

from apps.workflows.models import ExtractionTemplate, ExtractionTemplateField


class Command(BaseCommand):
    help = "Seed default system extraction templates."

    def handle(self, *args, **options):
        invoice_template, _ = ExtractionTemplate.objects.update_or_create(
            company=None,
            name="System Invoice Extraction",
            defaults={
                "template_type": ExtractionTemplate.TemplateType.INVOICE,
                "description": "Default invoice extraction template.",
                "is_system": True,
                "is_active": True,
                "instructions": "Extract key invoice fields from invoice documents.",
                "output_schema": {
                    "invoice_number": "text",
                    "vendor": "text",
                    "amount_due": "amount",
                    "gst": "amount",
                    "payment_terms": "text",
                },
            },
        )

        fields = [
            {
                "field_key": "invoice_number",
                "label": "Invoice Number",
                "field_type": ExtractionTemplateField.FieldType.TEXT,
                "regex_pattern": r"(?:Invoice Number|Invoice No|Invoice #)\s*[:\-]\s*([A-Za-z0-9\-\/]+)",
                "is_required": True,
                "order": 1,
            },
            {
                "field_key": "vendor",
                "label": "Vendor",
                "field_type": ExtractionTemplateField.FieldType.TEXT,
                "regex_pattern": r"(?:Vendor|Supplier)\s*[:\-]\s*(.+)",
                "is_required": True,
                "order": 2,
            },
            {
                "field_key": "amount_due",
                "label": "Amount Due",
                "field_type": ExtractionTemplateField.FieldType.AMOUNT,
                "regex_pattern": r"(?:Amount Due|Total Amount|Total)\s*[:\-]\s*([₹$€£]?\s?[0-9,]+(?:\.\d{1,2})?)",
                "is_required": True,
                "order": 3,
            },
            {
                "field_key": "gst",
                "label": "GST",
                "field_type": ExtractionTemplateField.FieldType.AMOUNT,
                "regex_pattern": r"(?:GST|Tax|VAT)\s*[:\-]\s*([0-9]+(?:\.\d+)?%?|[₹$€£]?\s?[0-9,]+(?:\.\d{1,2})?)",
                "is_required": False,
                "order": 4,
            },
            {
                "field_key": "payment_terms",
                "label": "Payment Terms",
                "field_type": ExtractionTemplateField.FieldType.TEXT,
                "regex_pattern": r"(?:Payment Terms|Terms)\s*[:\-]\s*(.+)",
                "is_required": False,
                "order": 5,
            },
        ]

        for field in fields:
            ExtractionTemplateField.objects.update_or_create(
                template=invoice_template,
                field_key=field["field_key"],
                defaults=field,
            )

        self.stdout.write(self.style.SUCCESS("Default extraction templates seeded."))
