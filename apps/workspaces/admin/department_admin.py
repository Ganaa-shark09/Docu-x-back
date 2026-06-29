from django.contrib import admin

from apps.workspaces.models import Department


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "department_type", "is_active", "created_at")
    list_filter = ("department_type", "is_active")
    search_fields = ("name", "company__name", "description")
    readonly_fields = ("uuid", "created_at", "updated_at")
