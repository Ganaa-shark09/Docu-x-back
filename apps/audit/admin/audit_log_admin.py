from django.contrib import admin

from apps.audit.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "event_type",
        "severity",
        "company",
        "user",
        "resource_type",
        "resource_uuid",
        "created_at",
    )
    list_filter = (
        "event_type",
        "severity",
        "company",
        "created_at",
    )
    search_fields = (
        "action",
        "company__name",
        "user__email",
        "user__username",
        "resource_type",
    )
    readonly_fields = (
        "uuid",
        "created_at",
        "updated_at",
    )
