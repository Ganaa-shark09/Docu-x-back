from django.db import migrations, models


def backfill_processing_state(apps, schema_editor):
    Document = apps.get_model("documents", "Document")

    Document.objects.filter(status="ready").update(
        processing_stage="ready",
        processing_progress=100,
    )
    Document.objects.filter(status="failed").update(
        processing_stage="failed",
    )
    Document.objects.filter(status="archived", is_ai_ready=True).update(
        processing_stage="ready",
        processing_progress=100,
    )
    Document.objects.filter(status="processing").update(
        processing_stage="extracting",
        processing_progress=10,
    )
    Document.objects.filter(status="uploaded").update(
        processing_stage="queued",
        processing_progress=0,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0002_document_scope_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="processing_stage",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("extracting", "Extracting"),
                    ("classifying", "Classifying"),
                    ("firewall_scan", "Firewall Scan"),
                    ("chunking", "Chunking"),
                    ("embedding", "Embedding"),
                    ("indexing", "Indexing"),
                    ("ready", "Ready"),
                    ("failed", "Failed"),
                ],
                db_index=True,
                default="queued",
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="processing_progress",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.RunPython(
            backfill_processing_state,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
