from rest_framework import serializers
from apps.ai_engine.models import CompanyModelGrant, LLMProviderConfig
from apps.tenants.models import Company


class CompanyModelGrantSerializer(serializers.ModelSerializer):
    company = serializers.SlugRelatedField(slug_field="uuid", queryset=Company.objects.all())
    provider_config = serializers.SlugRelatedField(
        slug_field="uuid", queryset=LLMProviderConfig.objects.filter(is_deleted=False)
    )
    provider_type = serializers.CharField(source="provider_config.provider_type", read_only=True)

    class Meta:
        model = CompanyModelGrant
        fields = (
            "uuid", "company", "provider_config", "provider_type", "model_id",
            "display_name", "is_enabled", "is_default", "created_at", "updated_at",
        )
        read_only_fields = ("uuid", "provider_type", "created_at", "updated_at")

    def validate(self, attrs):
        company = attrs.get("company", getattr(self.instance, "company", None))
        config = attrs.get("provider_config", getattr(self.instance, "provider_config", None))
        enabled = attrs.get("is_enabled", getattr(self.instance, "is_enabled", True))
        default = attrs.get("is_default", getattr(self.instance, "is_default", False))
        if company and config and config.company_id != company.pk:
            raise serializers.ValidationError("Model credentials must be owned by the selected company.")
        if default and not enabled:
            raise serializers.ValidationError("A disabled model cannot be selected as the default.")
        return attrs
