from django.contrib import admin

from apps.vector_search.models import DocumentChunk


@admin.register(DocumentChunk)
class DocumentChunkAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "company",
        "department",
        "chunk_index",
        "document_type",
        "sensitivity_label",
        "embedding_provider",
        "created_at",
    )
    list_filter = (
        "document_type",
        "sensitivity_label",
        "embedding_provider",
    )
    search_fields = (
        "document__title",
        "content",
        "company__name",
        "department__name",
    )
    readonly_fields = (
        "uuid",
        "created_at",
        "updated_at",
    )
