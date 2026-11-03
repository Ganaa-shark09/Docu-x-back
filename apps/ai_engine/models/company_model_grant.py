"""Explicit platform-issued AI model entitlements for a tenant."""
import re
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from apps.shared.models import BaseModel
from apps.tenants.models import Company

MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,149}$")


class CompanyModelGrant(BaseModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="ai_model_grants")
    provider_config = models.ForeignKey(
        "ai_engine.LLMProviderConfig", on_delete=models.PROTECT, related_name="model_grants"
    )
    model_id = models.CharField(max_length=150)
    display_name = models.CharField(max_length=160)
    is_enabled = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ("company_id", "provider_config_id", "model_id")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "provider_config", "model_id"),
                condition=Q(is_deleted=False), name="unique_live_company_ai_model",
            ),
            models.UniqueConstraint(
                fields=("company",), condition=Q(is_default=True, is_deleted=False),
                name="one_default_ai_model_per_company",
            ),
        ]
        indexes = [models.Index(fields=("company", "is_enabled"), name="ai_grant_company_enabled_idx")]

    def clean(self):
        if self.provider_config_id and self.company_id and self.provider_config.company_id != self.company_id:
            raise ValidationError({"provider_config": "Provider credentials must belong to the same company."})
        if self.provider_config_id:
            provider = self.provider_config.provider_type
            if provider == "azure_openai" and self.model_id != self.provider_config.deployment_name:
                raise ValidationError({"model_id": "Azure model grants must match the configured deployment name. Create a provider config for each deployment."})
            if provider == "local" and self.model_id != "local-extractive-rag":
                raise ValidationError({"model_id": "The local fallback only supports local-extractive-rag."})
        if not MODEL_ID_PATTERN.fullmatch(self.model_id or ""):
            raise ValidationError({"model_id": "Use a provider-issued model ID (letters, digits, . _ : / + -)."})
        if self.is_default and (not self.is_enabled or self.is_deleted):
            raise ValidationError({"is_default": "A disabled or deleted model cannot be the default."})

    def save(self, *args, **kwargs):
        if not self.is_default:
            self.full_clean()
            return super().save(*args, **kwargs)
        # Lock company row to serialize default changes across simultaneous administrators.
        with transaction.atomic():
            Company.objects.select_for_update().get(pk=self.company_id)
            type(self).objects.filter(company_id=self.company_id, is_default=True, is_deleted=False).exclude(pk=self.pk).update(is_default=False)
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.company} · {self.provider_config.provider_type} · {self.model_id}"
