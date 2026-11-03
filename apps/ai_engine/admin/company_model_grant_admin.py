from django.contrib import admin
from django.utils import timezone
from apps.ai_engine.models import CompanyModelGrant


@admin.register(CompanyModelGrant)
class CompanyModelGrantAdmin(admin.ModelAdmin):
    """Platform superusers grant provider/model combinations per company."""
    list_display = ("company", "provider_config", "model_id", "is_enabled", "is_default", "updated_at")
    list_filter = ("company", "is_enabled", "is_default", "provider_config__provider_type")
    search_fields = ("company__name", "model_id", "display_name")
    readonly_fields = ("uuid", "created_at", "updated_at")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


    def delete_model(self, request, obj):
        # Keep the historical entitlement row; revocation must not reenable
        # unrestricted legacy routing for a tenant.
        obj.is_default = False
        obj.is_enabled = False
        obj.save(update_fields=["is_default", "is_enabled", "updated_at"])
        obj.soft_delete()

    def delete_queryset(self, request, queryset):
        queryset.update(is_default=False, is_enabled=False, is_deleted=True, deleted_at=timezone.now())
