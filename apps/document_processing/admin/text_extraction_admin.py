from django.contrib import admin

from apps.document_processing.models import DocumentTextExtraction


@admin.register(DocumentTextExtraction)
class DocumentTextExtractionAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "extraction_method",
        "page_count",
        "character_count",
        "word_count",
        "confidence_score",
        "created_at",
    )
    list_filter = ("extraction_method",)
    search_fields = ("document__title", "extracted_text")
    readonly_fields = ("uuid", "created_at", "updated_at")
