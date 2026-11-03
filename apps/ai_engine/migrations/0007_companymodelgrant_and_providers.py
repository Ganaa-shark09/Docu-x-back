import uuid
import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [("ai_engine", "0006_aiconversationdocument"), ("tenants", "0001_initial")]
    operations = [
        migrations.AlterField(
            model_name="llmproviderconfig", name="provider_type",
            field=models.CharField(
                choices=[
                    ("local", "Local Fallback"),
                    ("ollama", "Ollama Local LLM"),
                    ("openai", "OpenAI"),
                    ("azure_openai", "Azure OpenAI"),
                    ("groq", "Groq"),
                    ("gemini", "Google Gemini"),
                    ("anthropic", "Anthropic Claude"),
                ], default="local", db_index=True, max_length=50,
            ),
        ),
        migrations.CreateModel(
            name="CompanyModelGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)),
                ("created_at", models.DateTimeField(default=timezone.now, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_deleted", models.BooleanField(default=False, db_index=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("model_id", models.CharField(max_length=150)),
                ("display_name", models.CharField(max_length=160)),
                ("is_enabled", models.BooleanField(default=True)),
                ("is_default", models.BooleanField(default=False)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_model_grants", to="tenants.company")),
                ("provider_config", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="model_grants", to="ai_engine.llmproviderconfig")),
            ],
            options={"ordering": ("company_id", "provider_config_id", "model_id")},
        ),
        migrations.AddConstraint(
            model_name="companymodelgrant",
            constraint=models.UniqueConstraint(
                fields=("company", "provider_config", "model_id"),
                condition=Q(is_deleted=False), name="unique_live_company_ai_model",
            ),
        ),
        migrations.AddConstraint(
            model_name="companymodelgrant",
            constraint=models.UniqueConstraint(
                fields=("company",), condition=Q(is_default=True, is_deleted=False),
                name="one_default_ai_model_per_company",
            ),
        ),
        migrations.AddIndex(
            model_name="companymodelgrant",
            index=models.Index(fields=("company", "is_enabled"), name="ai_grant_company_enabled_idx"),
        ),
    ]
