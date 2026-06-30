from django import forms
from django.contrib import admin

from apps.ai_engine.models import LLMProviderConfig


class LLMProviderConfigAdminForm(forms.ModelForm):
    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Optional. Leave blank to keep existing key. Stored encrypted.",
    )

    class Meta:
        model = LLMProviderConfig
        fields = [
            "company",
            "name",
            "provider_type",
            "is_active",
            "is_default",
            "model_name",
            "endpoint_url",
            "deployment_name",
            "api_version",
            "api_key",
            "max_tokens",
            "temperature",
            "allow_sensitive_context",
            "notes",
        ]

    def save(self, commit=True):
        instance = super().save(commit=False)
        api_key = self.cleaned_data.get("api_key")

        if api_key:
            instance.set_api_key(api_key)

        if commit:
            instance.save()

        return instance


@admin.register(LLMProviderConfig)
class LLMProviderConfigAdmin(admin.ModelAdmin):
    form = LLMProviderConfigAdminForm

    list_display = (
        "name",
        "company",
        "provider_type",
        "model_name",
        "deployment_name",
        "is_default",
        "is_active",
        "created_at",
    )
    list_filter = ("provider_type", "is_default", "is_active")
    search_fields = ("name", "company__name", "model_name", "deployment_name")
    readonly_fields = ("uuid", "created_at", "updated_at")
