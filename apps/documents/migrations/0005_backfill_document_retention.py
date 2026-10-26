from datetime import timedelta

from django.db import migrations


def backfill_retention(apps, schema_editor):
    Document = apps.get_model("documents", "Document")
    batch = []
    for document in (
        Document.objects.using(schema_editor.connection.alias)
        .filter(retention_start__isnull=True)
        .select_related("company")
        .iterator(chunk_size=1000)
    ):
        document.retention_start = document.created_at
        document.retention_duration_days = max(1, document.company.data_retention_days)
        document.retention_expires_at = document.retention_start + timedelta(
            days=document.retention_duration_days
        )
        batch.append(document)
        if len(batch) >= 1000:
            Document.objects.using(schema_editor.connection.alias).bulk_update(
                batch,
                ["retention_start", "retention_duration_days", "retention_expires_at"],
            )
            batch = []
    if batch:
        Document.objects.using(schema_editor.connection.alias).bulk_update(
            batch,
            ["retention_start", "retention_duration_days", "retention_expires_at"],
        )


class Migration(migrations.Migration):
    dependencies = [("documents", "0004_sensitivitypolicy_document_custodian_and_more")]
    operations = [migrations.RunPython(backfill_retention, migrations.RunPython.noop)]
