from django.contrib import admin

from apps.workspaces.models import DepartmentMembership


@admin.register(DepartmentMembership)
class DepartmentMembershipAdmin(admin.ModelAdmin):
    list_display = (
        "department",
        "user",
        "company",
        "role",
        "is_active",
        "created_at",
    )
    list_filter = ("role", "is_active", "company")
    search_fields = (
        "department__name",
        "user__username",
        "user__email",
        "company__name",
    )
    readonly_fields = ("uuid", "created_at", "updated_at")
