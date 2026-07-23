from django.contrib import admin

from apps.notifications.models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "recipient",
        "company",
        "notification_type",
        "status",
        "priority",
        "created_at",
    )
    list_filter = (
        "notification_type",
        "status",
        "priority",
        "company",
        "created_at",
    )
    search_fields = (
        "title",
        "message",
        "recipient__email",
        "recipient__username",
        "company__name",
        "resource_type",
    )
    readonly_fields = (
        "uuid",
        "created_at",
        "updated_at",
        "read_at",
        "archived_at",
    )
