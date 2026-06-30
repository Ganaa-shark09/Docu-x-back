from rest_framework import serializers

from apps.ai_engine.models import LLMProviderConfig


class LLMProviderConfigSerializer(serializers.ModelSerializer):
    has_api_key = serializers.SerializerMethodField()
    api_key = serializers.CharField(write_only=True, required=False, allow_blank=True)

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

    def get_has_api_key(self, obj):
        return bool(obj.api_key_encrypted)

    def create(self, validated_data):
        api_key = validated_data.pop("api_key", "")
        request = self.context["request"]

        config = LLMProviderConfig(
            company=request.user.company,
            **validated_data,
        )

        if api_key:
            config.set_api_key(api_key)

        config.save()
        return config

    def update(self, instance, validated_data):
        api_key = validated_data.pop("api_key", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if api_key:
            instance.set_api_key(api_key)

        instance.save()
        return instance
