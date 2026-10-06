from rest_framework import serializers

from apps.ai_engine.models import LLMProviderConfig
from apps.ai_engine.services.llm_provider_security_service import (
    LLMProviderSecurityService,
)


class LLMProviderConfigSerializer(serializers.ModelSerializer):
    has_api_key = serializers.SerializerMethodField()

    api_key = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        trim_whitespace=False,
    )

    class Meta:
        model = LLMProviderConfig
        fields = [
            "uuid",
            "name",
            "provider_type",
            "is_active",
            "is_default",
            "model_name",
            "endpoint_url",
            "deployment_name",
            "api_version",
            "api_key",
            "has_api_key",
            "max_tokens",
            "temperature",
            "allow_sensitive_context",
            "notes",
            "created_at",
            "updated_at",
        ]

        read_only_fields = [
            "uuid",
            "has_api_key",
            "created_at",
            "updated_at",
        ]

    def get_has_api_key(self, obj) -> bool:
        return bool(obj.api_key_encrypted)

    def validate(self, attrs):
        instance = self.instance

        provider_type = attrs.get(
            "provider_type",
            getattr(instance, "provider_type", None),
        )

        endpoint_url = attrs.get(
            "endpoint_url",
            getattr(instance, "endpoint_url", ""),
        )

        deployment_name = attrs.get(
            "deployment_name",
            getattr(instance, "deployment_name", ""),
        )

        api_version = attrs.get(
            "api_version",
            getattr(instance, "api_version", ""),
        )

        try:
            if provider_type == LLMProviderConfig.ProviderType.OPENAI:
                # OpenAI endpoint is intentionally fixed in the gateway.
                attrs["endpoint_url"] = LLMProviderSecurityService.validate_endpoint(
                    provider_type,
                    "",
                )

            elif provider_type == LLMProviderConfig.ProviderType.AZURE_OPENAI:
                attrs["endpoint_url"] = LLMProviderSecurityService.validate_endpoint(
                    provider_type,
                    endpoint_url,
                )

                attrs["deployment_name"] = (
                    LLMProviderSecurityService.validate_azure_deployment_name(
                        deployment_name
                    )
                )

                attrs["api_version"] = (
                    LLMProviderSecurityService.validate_azure_api_version(api_version)
                )

            elif provider_type == LLMProviderConfig.ProviderType.OLLAMA:
                if endpoint_url:
                    attrs["endpoint_url"] = (
                        LLMProviderSecurityService.validate_endpoint(
                            provider_type,
                            endpoint_url,
                        )
                    )

            elif provider_type == LLMProviderConfig.ProviderType.LOCAL:
                attrs["endpoint_url"] = ""

        except ValueError as exc:
            raise serializers.ValidationError({"endpoint_url": str(exc)}) from exc

        supplied_api_key = attrs.get("api_key")

        if (
            provider_type
            in {
                LLMProviderConfig.ProviderType.LOCAL,
                LLMProviderConfig.ProviderType.OLLAMA,
            }
            and supplied_api_key
        ):
            raise serializers.ValidationError(
                {"api_key": ("API keys are not accepted for local/Ollama providers.")}
            )

        return attrs

    def create(self, validated_data):
        api_key = validated_data.pop("api_key", "")
        request = self.context["request"]

        company = getattr(request.user, "company", None)

        if company is None:
            raise serializers.ValidationError(
                {
                    "company": (
                        "A company context is required to create " "an LLM provider."
                    )
                }
            )

        config = LLMProviderConfig(
            company=company,
            **validated_data,
        )

        if api_key:
            config.set_api_key(api_key)

        config.save()

        return config

    def update(self, instance, validated_data):
        api_key = validated_data.pop("api_key", None)

        old_provider = instance.provider_type
        old_endpoint = (instance.endpoint_url or "").rstrip("/")

        new_provider = validated_data.get(
            "provider_type",
            old_provider,
        )

        new_endpoint = (
            validated_data.get(
                "endpoint_url",
                instance.endpoint_url,
            )
            or ""
        ).rstrip("/")

        credential_target_changed = old_provider != new_provider or (
            new_provider == LLMProviderConfig.ProviderType.AZURE_OPENAI
            and old_endpoint != new_endpoint
        )

        cloud_provider = new_provider in {
            LLMProviderConfig.ProviderType.OPENAI,
            LLMProviderConfig.ProviderType.AZURE_OPENAI,
        }

        if (
            cloud_provider
            and credential_target_changed
            and instance.api_key_encrypted
            and not api_key
        ):
            raise serializers.ValidationError(
                {
                    "api_key": (
                        "Re-enter the API key when changing the cloud "
                        "provider or Azure endpoint."
                    )
                }
            )

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if new_provider in {
            LLMProviderConfig.ProviderType.LOCAL,
            LLMProviderConfig.ProviderType.OLLAMA,
        }:
            # Never leave an unused cloud credential attached to a
            # local provider configuration.
            instance.api_key_encrypted = ""

        elif api_key:
            instance.set_api_key(api_key)

        instance.save()

        return instance
