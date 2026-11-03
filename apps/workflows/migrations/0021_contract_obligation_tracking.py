"""Evidence-anchored, human-owned contract follow-up records."""

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0020_invoiceaccountinghandoff"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ContractObligationAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("uuid", models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ("is_deleted", models.BooleanField(db_index=True, default=False)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("source_revision", models.PositiveIntegerField()),
                ("source_index", models.PositiveIntegerField()),
                ("source_digest", models.CharField(max_length=64)),
                ("source_snapshot", models.JSONField(default=dict)),
                ("governance_revision", models.PositiveIntegerField()),
                ("status", models.CharField(
                    choices=[("open", "Open"), ("in_progress", "In progress"),
                             ("completed", "Completed (attested)")],
                    max_length=20, default="open",
                )),
                ("revision", models.PositiveIntegerField(default=1)),
                ("company", models.ForeignKey(to="tenants.company", on_delete=django.db.models.deletion.PROTECT)),
                ("review", models.ForeignKey(to="workflows.contractreview", related_name="obligation_assignments", on_delete=django.db.models.deletion.PROTECT)),
                ("owner", models.ForeignKey(to=settings.AUTH_USER_MODEL, related_name="contract_obligations", on_delete=django.db.models.deletion.PROTECT)),
                ("created_by", models.ForeignKey(to=settings.AUTH_USER_MODEL, related_name="contract_obligations_created", null=True, on_delete=django.db.models.deletion.SET_NULL)),
            ],
            options={
                "constraints": [models.UniqueConstraint(
                    fields=("review", "source_revision", "source_index"),
                    name="contract_obligation_source_unique",
                )],
                "indexes": [models.Index(fields=["company", "status"], name="contract_ob_company_a35396_idx")],
            },
        ),
        migrations.CreateModel(
            name="ContractObligationEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("uuid", models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True)),
                ("is_deleted", models.BooleanField(db_index=True, default=False)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("sequence", models.PositiveIntegerField()),
                ("from_status", models.CharField(max_length=20, blank=True)),
                ("to_status", models.CharField(max_length=20)),
                ("reason", models.CharField(max_length=500)),
                ("obligation", models.ForeignKey(to="workflows.contractobligationassignment", related_name="events", on_delete=django.db.models.deletion.PROTECT)),
                ("actor", models.ForeignKey(to=settings.AUTH_USER_MODEL, null=True, on_delete=django.db.models.deletion.SET_NULL)),
            ],
            options={
                "ordering": ["sequence"],
                "constraints": [models.UniqueConstraint(
                    fields=("obligation", "sequence"),
                    name="contract_obligation_event_sequence",
                )],
            },
        ),
    ]
