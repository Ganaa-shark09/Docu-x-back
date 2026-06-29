from django.contrib import admin

from apps.data_firewall.models import FirewallScan


@admin.register(FirewallScan)
class FirewallScanAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "risk_level",
        "decision",
        "can_use_cloud_llm",
        "requires_human_approval",
        "created_at",
    )
    list_filter = (
        "risk_level",
        "decision",
        "can_use_cloud_llm",
        "requires_human_approval",
    )
    search_fields = ("document__title", "scan_summary")
    readonly_fields = ("uuid", "created_at", "updated_at")
