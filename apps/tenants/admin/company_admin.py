from django.contrib import admin

from apps.tenants.models import Company


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "status",
        "country",
        "allow_cloud_llm",
        "created_at",
    )
    list_filter = ("status", "allow_cloud_llm", "allow_local_llm", "country")
    search_fields = ("name", "slug", "legal_name")
    readonly_fields = ("uuid", "created_at", "updated_at")
