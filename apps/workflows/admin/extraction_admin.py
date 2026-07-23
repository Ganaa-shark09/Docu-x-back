from django.contrib import admin

from apps.workflows.models import (
    ExtractionResultField,
    ExtractionRun,
    ExtractionTemplate,
    ExtractionTemplateField,
)


class ExtractionTemplateFieldInline(admin.TabularInline):
    model = ExtractionTemplateField
    extra = 0
    readonly_fields = ("uuid", "created_at", "updated_at")


@admin.register(ExtractionTemplate)
class ExtractionTemplateAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "company",
        "template_type",
        "is_system",
        "is_active",
        "created_at",
    )
    list_filter = ("template_type", "is_system", "is_active")
    search_fields = ("name", "description", "company__name")
    readonly_fields = ("uuid", "created_at", "updated_at")
    inlines = [ExtractionTemplateFieldInline]


@admin.register(ExtractionTemplateField)
class ExtractionTemplateFieldAdmin(admin.ModelAdmin):
    list_display = (
        "template",
        "field_key",
        "label",
        "field_type",
        "is_required",
        "order",
    )
    list_filter = ("field_type", "is_required")
    search_fields = ("template__name", "field_key", "label")
    readonly_fields = ("uuid", "created_at", "updated_at")


class ExtractionResultFieldInline(admin.TabularInline):
    model = ExtractionResultField
    extra = 0
    readonly_fields = ("uuid", "created_at", "updated_at")


@admin.register(ExtractionRun)
class ExtractionRunAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "template",
        "company",
        "status",
        "overall_confidence",
        "requested_by",
        "created_at",
    )
    list_filter = ("status", "template", "company")
    search_fields = ("document__title", "template__name", "requested_by__email")
    readonly_fields = ("uuid", "created_at", "updated_at")
    inlines = [ExtractionResultFieldInline]


@admin.register(ExtractionResultField)
class ExtractionResultFieldAdmin(admin.ModelAdmin):
    list_display = (
        "extraction_run",
        "field_key",
        "label",
        "confidence_score",
        "is_missing",
        "is_corrected",
    )
    list_filter = ("is_missing", "is_corrected")
    search_fields = ("field_key", "label", "extracted_value", "corrected_value")
    readonly_fields = ("uuid", "created_at", "updated_at")
