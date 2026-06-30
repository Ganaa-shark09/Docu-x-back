import os

from apps.ai_engine.models import LLMProviderConfig


class LLMProviderConfigService:
    @classmethod
    def get_default_config_for_company(cls, company):
        config = (
            LLMProviderConfig.objects.filter(
                company=company,
                is_active=True,
                is_default=True,
            )
            .order_by("-updated_at")
            .first()
        )

        if config:
            return config

        return cls._build_env_fallback_config(company)

    @classmethod
    def _build_env_fallback_config(cls, company):
        provider = os.getenv("DEFAULT_LLM_PROVIDER", "local").lower()

        if provider == "openai" and os.getenv("OPENAI_API_KEY"):
            config = LLMProviderConfig(
                company=company,
                name="Environment OpenAI",
                provider_type=LLMProviderConfig.ProviderType.OPENAI,
                model_name=os.getenv("OPENAI_DEFAULT_MODEL", "gpt-4.1-mini"),
                max_tokens=1200,
                temperature=0.2,
                is_active=True,
                is_default=True,
            )
            config.set_api_key(os.getenv("OPENAI_API_KEY"))
            return config

        if provider == "azure_openai" and os.getenv("AZURE_OPENAI_API_KEY"):
            config = LLMProviderConfig(
                company=company,
                name="Environment Azure OpenAI",
                provider_type=LLMProviderConfig.ProviderType.AZURE_OPENAI,
                endpoint_url=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
                deployment_name=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", ""),
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
                max_tokens=1200,
                temperature=0.2,
                is_active=True,
                is_default=True,
            )
            config.set_api_key(os.getenv("AZURE_OPENAI_API_KEY"))
            return config

        return LLMProviderConfig(
            company=company,
            name="Local Fallback",
            provider_type=LLMProviderConfig.ProviderType.LOCAL,
            model_name="local-extractive-rag",
            max_tokens=1200,
            temperature=0.2,
            is_active=True,
            is_default=True,
        )
