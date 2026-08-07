from django.contrib import admin

from apps.ai_engine.models import AIConversationDocument


@admin.register(AIConversationDocument)
class AIConversationDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "conversation",
        "document",
        "company",
        "uploaded_by",
        "created_at",
    )
    list_filter = ("company", "created_at")
    search_fields = (
        "conversation__uuid",
        "document__title",
        "document__original_filename",
        "uploaded_by__email",
    )
    readonly_fields = ("uuid", "created_at", "updated_at")
