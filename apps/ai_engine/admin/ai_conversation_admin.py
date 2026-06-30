from django.contrib import admin

from apps.ai_engine.models import AIConversation, AIMessage


class AIMessageInline(admin.TabularInline):
    model = AIMessage
    extra = 0
    readonly_fields = (
        "uuid",
        "role",
        "content",
        "retrieved_sources",
        "model_provider",
        "model_name",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "created_at",
        "updated_at",
    )
    can_delete = False


@admin.register(AIConversation)
class AIConversationAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "company",
        "user",
        "department",
        "status",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "department")
    search_fields = ("title", "user__email", "user__username", "company__name")
    readonly_fields = ("uuid", "created_at", "updated_at")
    inlines = [AIMessageInline]


@admin.register(AIMessage)
class AIMessageAdmin(admin.ModelAdmin):
    list_display = (
        "conversation",
        "role",
        "model_provider",
        "model_name",
        "total_tokens",
        "created_at",
    )
    list_filter = ("role", "model_provider", "model_name")
    search_fields = ("content", "conversation__title")
    readonly_fields = ("uuid", "created_at", "updated_at")
