from django.db import migrations

FIELDS = [
    {
        "field_key": "invoice_number",
        "label": "Invoice Number",
        "regex_pattern": "^[ \\t]*(?:invoice\\ number|invoice\\ no|invoice\\ \\#)[ "
        "\\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": True,
        "extraction_hint": "",
        "default_value": "",
        "order": 0,
    },
    {
        "field_key": "vendor_name",
        "label": "Vendor Name",
        "regex_pattern": "^[ \\t]*(?:vendor\\ name|vendor|supplier)[ \\t]*[:=][ "
        "\\t]*([^\\r\\n]+)",
        "is_required": True,
        "extraction_hint": "",
        "default_value": "",
        "order": 1,
    },
    {
        "field_key": "vendor_identifier",
        "label": "Vendor Identifier",
        "regex_pattern": "^[ \\t]*(?:vendor\\ identifier|vendor\\ id|supplier\\ id)[ "
        "\\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 2,
    },
    {
        "field_key": "vendor_tax_id",
        "label": "Vendor Tax Id",
        "regex_pattern": "^[ \\t]*(?:vendor\\ tax\\ id|tax\\ id|gstin|vat\\ id)[ "
        "\\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 3,
    },
    {
        "field_key": "vendor_address",
        "label": "Vendor Address",
        "regex_pattern": "^[ \\t]*(?:vendor\\ address)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 4,
    },
    {
        "field_key": "bill_to_name",
        "label": "Bill To Name",
        "regex_pattern": "^[ \\t]*(?:bill\\ to\\ name)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 5,
    },
    {
        "field_key": "bill_to_address",
        "label": "Bill To Address",
        "regex_pattern": "^[ \\t]*(?:bill\\ to\\ address)[ \\t]*[:=][ "
        "\\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 6,
    },
    {
        "field_key": "purchase_order_number",
        "label": "Purchase Order Number",
        "regex_pattern": "^[ \\t]*(?:purchase\\ order\\ number|po\\ number|po\\ no)[ "
        "\\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 7,
    },
    {
        "field_key": "payment_terms",
        "label": "Payment Terms",
        "regex_pattern": "^[ \\t]*(?:payment\\ terms)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 8,
    },
    {
        "field_key": "payment_method",
        "label": "Payment Method",
        "regex_pattern": "^[ \\t]*(?:payment\\ method)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 9,
    },
    {
        "field_key": "invoice_reference",
        "label": "Invoice Reference",
        "regex_pattern": "^[ \\t]*(?:invoice\\ reference)[ \\t]*[:=][ "
        "\\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 10,
    },
    {
        "field_key": "customer_reference",
        "label": "Customer Reference",
        "regex_pattern": "^[ \\t]*(?:customer\\ reference|account\\ reference)[ "
        "\\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 11,
    },
    {
        "field_key": "invoice_date",
        "label": "Invoice Date",
        "regex_pattern": "^[ \\t]*(?:invoice\\ date)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": True,
        "extraction_hint": "",
        "default_value": "",
        "order": 12,
    },
    {
        "field_key": "due_date",
        "label": "Due Date",
        "regex_pattern": "^[ \\t]*(?:due\\ date)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 13,
    },
    {
        "field_key": "currency",
        "label": "Currency",
        "regex_pattern": "^[ \\t]*(?:currency)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": True,
        "extraction_hint": "",
        "default_value": "",
        "order": 14,
    },
    {
        "field_key": "subtotal",
        "label": "Subtotal",
        "regex_pattern": "^[ \\t]*(?:subtotal)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": True,
        "extraction_hint": "",
        "default_value": "",
        "order": 15,
    },
    {
        "field_key": "tax_total",
        "label": "Tax Total",
        "regex_pattern": "^[ \\t]*(?:tax\\ total)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 16,
    },
    {
        "field_key": "discount_total",
        "label": "Discount Total",
        "regex_pattern": "^[ \\t]*(?:discount\\ total)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 17,
    },
    {
        "field_key": "shipping_total",
        "label": "Shipping Total",
        "regex_pattern": "^[ \\t]*(?:shipping\\ total)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 18,
    },
    {
        "field_key": "other_charges",
        "label": "Other Charges",
        "regex_pattern": "^[ \\t]*(?:other\\ charges)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 19,
    },
    {
        "field_key": "grand_total",
        "label": "Grand Total",
        "regex_pattern": "^[ \\t]*(?:grand\\ total|invoice\\ total|total\\ amount)[ "
        "\\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": True,
        "extraction_hint": "",
        "default_value": "",
        "order": 20,
    },
    {
        "field_key": "amount_due",
        "label": "Amount Due",
        "regex_pattern": "^[ \\t]*(?:amount\\ due)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 21,
    },
    {
        "field_key": "line_items",
        "label": "Line Items",
        "regex_pattern": "^[ \\t]*(?:line\\ items)[ \\t]*[:=][ \\t]*([^\\r\\n]+)",
        "is_required": False,
        "extraction_hint": "",
        "default_value": "",
        "order": 22,
    },
]


def seed(apps, schema_editor):
    Template = apps.get_model("workflows", "ExtractionTemplate")
    Field = apps.get_model("workflows", "ExtractionTemplateField")
    template, _ = Template.objects.get_or_create(
        company=None,
        is_system=True,
        name="Canonical Invoice Schema v1",
        defaults={"template_type": "invoice", "output_schema": {"schema_version": 1}},
    )
    for spec in FIELDS:
        key = spec["field_key"]
        spec = {
            **spec,
            "field_type": "date"
            if key in ("invoice_date", "due_date")
            else "amount"
            if key
            in (
                "subtotal",
                "tax_total",
                "discount_total",
                "shipping_total",
                "other_charges",
                "grand_total",
                "amount_due",
            )
            else "json"
            if key == "line_items"
            else "text",
        }
        Field.objects.get_or_create(
            template=template,
            field_key=spec["field_key"],
            defaults={k: v for k, v in spec.items() if k != "field_key"},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0005_alter_workflowstep_step_type_extractioncorrection_and_more")
    ]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
