from django.db import migrations


def seed(apps, schema_editor):
    ClauseType = apps.get_model("workflows", "ClauseType")
    # Definitions describe facts only. Tenants publish their own legal risk policy.
    definitions = {
        "confidentiality": (
            "Confidentiality",
            {
                "duration_months": {"type": "decimal"},
                "return_required": {"type": "boolean"},
            },
        ),
        "limitation_of_liability": (
            "Limitation of Liability",
            {
                "cap_amount": {"type": "decimal"},
                "cap_months": {"type": "decimal"},
                "cap_basis": {"type": "text"},
                "uncapped": {"type": "boolean"},
            },
        ),
        "termination": (
            "Termination",
            {
                "notice_days": {"type": "decimal"},
                "cure_days": {"type": "decimal"},
                "convenience_allowed": {"type": "boolean"},
            },
        ),
        "data_protection": (
            "Data Protection",
            {
                "breach_notice_hours": {"type": "decimal"},
                "subprocessors_allowed": {"type": "boolean"},
                "role": {"type": "enum", "values": ["processor", "controller"]},
            },
        ),
        "payment": ("Payment", {"due_days": {"type": "decimal"}}),
        "renewal": (
            "Renewal",
            {"renewal_months": {"type": "decimal"}, "automatic": {"type": "boolean"}},
        ),
        "governing_law": ("Governing Law", {"jurisdiction": {"type": "text"}}),
        "indemnification": ("Indemnification", {}),
        "dispute_resolution": ("Dispute Resolution", {}),
        "intellectual_property": ("Intellectual Property", {}),
        "security": ("Security", {}),
        "audit_rights": ("Audit Rights", {}),
        "assignment": ("Assignment", {}),
        "warranty": ("Warranty", {}),
        "service_levels": ("Service Levels", {}),
        "insurance": ("Insurance", {}),
        "force_majeure": ("Force Majeure", {}),
        "notices": ("Notices", {}),
        "exclusivity": ("Exclusivity", {}),
        "non_solicitation": ("Non Solicitation", {}),
        "non_compete": ("Non Compete", {}),
    }
    for code, (name, schema) in definitions.items():
        ClauseType.objects.get_or_create(
            company=None,
            code=code,
            defaults={
                "name": name,
                "category": code,
                "fact_schema": schema,
                "aliases": [],
                "document_types": [],
            },
        )


class Migration(migrations.Migration):
    dependencies = [("workflows", "0009_clauseplaybook_clauseplaybookversion_and_more")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
