from django.db import models

from apps.shared.models import BaseModel
from apps.shared.services import EncryptionService
from apps.tenants.models import Company


class LLMProviderConfig(BaseModel):
    class ProviderType(models.TextChoices):
        LOCAL = "local", "Local Fallback"
        OLLAMA = "ollama", "Ollama Local LLM"
        OPENAI = "openai", "OpenAI"
        AZURE_OPENAI = "azure_openai", "Azure OpenAI"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="llm_provider_configs",
    )

    name = models.CharField(max_length=150)
    provider_type = models.CharField(
        max_length=50,
        choices=ProviderType.choices,
        default=ProviderType.LOCAL,
        db_index=True,
    )

    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)

    model_name = models.CharField(
        max_length=150,
        blank=True,
        help_text="OpenAI model name or local model name.",
    )

    endpoint_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="LLM endpoint URL. Supports internal Docker URLs like http://ollama:11434.",
    )

    deployment_name = models.CharField(
        max_length=150,
        blank=True,
        help_text="Azure OpenAI deployment name.",
    )

    api_version = models.CharField(
        max_length=50,
        blank=True,
        help_text="Azure OpenAI API version.",
    )

    api_key_encrypted = models.TextField(blank=True)

    max_tokens = models.PositiveIntegerField(default=1200)
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=0.20)

    allow_sensitive_context = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    def set_api_key(self, api_key):
        self.api_key_encrypted = EncryptionService.encrypt(api_key)

    def get_api_key(self):
        return EncryptionService.decrypt(self.api_key_encrypted)

    def save(self, *args, **kwargs):
        if self.is_default:
            LLMProviderConfig.objects.filter(
                company=self.company,
                is_default=True,
            ).exclude(pk=self.pk).update(is_default=False)

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.company.name} - {self.name} ({self.provider_type})"

    class Meta:
        ordering = ["company", "-is_default", "name"]
        indexes = [
            models.Index(fields=["company", "provider_type"]),
            models.Index(fields=["company", "is_default"]),
            models.Index(fields=["company", "is_active"]),
        ]
