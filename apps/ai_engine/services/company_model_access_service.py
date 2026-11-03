"""Tenant and user model-selection policy. Never trust model IDs from clients."""
from copy import copy
import os
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, ValidationError
from apps.ai_engine.models import CompanyModelGrant


class CompanyModelAccessService:
    @classmethod
    def available(cls, company):
        qs = CompanyModelGrant.objects.filter(
            company=company, is_enabled=True, is_deleted=False,
            provider_config__company=company,
            provider_config__is_active=True,
            provider_config__is_deleted=False,
        ).select_related("provider_config").order_by("-is_default", "display_name", "pk")
        if not company.allow_cloud_llm:
            qs = qs.exclude(provider_config__provider_type__in=[
                "openai", "azure_openai", "groq", "gemini", "anthropic"
            ])
        if not company.allow_local_llm:
            qs = qs.exclude(provider_config__provider_type__in=["ollama", "local"])
        # Never advertise an API-backed model without a configured encrypted credential.
        cloud = ["openai", "azure_openai", "groq", "gemini", "anthropic"]
        qs = qs.exclude(Q(provider_config__provider_type__in=cloud) & Q(provider_config__api_key_encrypted=""))
        # Azure deployments and credentials must be present before displaying a grant.
        qs = qs.exclude(
            Q(provider_config__provider_type="azure_openai")
            & (Q(provider_config__endpoint_url="") | Q(provider_config__deployment_name=""))
        )
        return qs

    @classmethod
    def resolve(cls, user, model_uuid=None):
        """Return (effective provider config, grant) or (None, None) for legacy tenants.

        When tenant grants exist, missing/invalid selection never falls through to an
        unconstrained env model. Credentials and endpoints come from DB, not request.
        """
        if not user.company_id:
            raise PermissionDenied("A company membership is required.")
        company = user.company
        configured = CompanyModelGrant.objects.filter(company=company).exists()
        if not configured and not model_uuid:
            if os.getenv("DOCUX_ENFORCE_MODEL_GRANTS", "false").lower() in ("1", "true", "yes"):
                raise PermissionDenied("No AI model entitlements have been configured for this company.")
            return None, None  # migration-safe legacy fallback until enforcement enabled
        available = cls.available(company)
        if model_uuid:
            grant = available.filter(uuid=model_uuid).first()
            if not grant:
                raise ValidationError({"model_uuid": "This model is not approved or available to your company."})
        else:
            grant = available.first()
            if grant is None:
                raise PermissionDenied("No AI models are currently enabled for this company.")
        # Clone, never mutate shared provider configuration. The granted ID is
        # authoritative even if a provider config's default model is different.
        config = copy(grant.provider_config)
        config.model_name = grant.model_id
        config._entitlement_enforced = True
        return config, grant
