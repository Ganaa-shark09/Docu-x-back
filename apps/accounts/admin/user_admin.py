from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "company", "role", "is_active", "is_staff")
    list_filter = ("role", "is_active", "is_staff", "is_superuser")
    search_fields = ("username", "email", "first_name", "last_name", "company__name")
    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "DocuX",
            {
                "fields": (
                    "company",
                    "role",
                    "phone_number",
                    "job_title",
                    "is_email_verified",
                )
            },
        ),
    )
