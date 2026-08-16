from django.contrib import admin

from apps.documents.models import (
    Document,
    DocumentAccessPolicy,
    DocumentProcessingJob,
    DocumentVersion,
)


class DocumentVersionInline(admin.TabularInline):
    model = DocumentVersion
    extra = 0
    readonly_fields = ("uuid", "created_at", "updated_at")


class DocumentProcessingJobInline(admin.TabularInline):
    model = DocumentProcessingJob
    extra = 0
    readonly_fields = ("uuid", "created_at", "updated_at")


class DocumentAccessPolicyInline(admin.TabularInline):
    model = DocumentAccessPolicy
    extra = 0
    readonly_fields = ("uuid", "created_at", "updated_at")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "company",
        "department",
        "document_type",
        "sensitivity_label",
        "status",
        "processing_stage",
        "processing_progress",
        "is_ai_ready",
        "created_at",
    )
    list_filter = (
        "document_type",
        "sensitivity_label",
        "status",
        "processing_stage",
        "is_indexed",
        "is_ocr_completed",
        "is_ai_ready",
    )
    search_fields = (
        "title",
        "original_filename",
        "company__name",
        "department__name",
    )
    readonly_fields = ("uuid", "created_at", "updated_at")
    inlines = [
        DocumentVersionInline,
        DocumentAccessPolicyInline,
        DocumentProcessingJobInline,
    ]


@admin.register(DocumentVersion)
class DocumentVersionAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "version_number",
        "original_filename",
        "uploaded_by",
        "created_at",
    )
    search_fields = ("document__title", "original_filename")
    readonly_fields = ("uuid", "created_at", "updated_at")


@admin.register(DocumentAccessPolicy)
class DocumentAccessPolicyAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "user",
        "role",
        "access_level",
        "can_download",
        "can_export",
        "can_use_ai",
    )
    list_filter = ("access_level", "can_download", "can_export", "can_use_ai")
    search_fields = ("document__title", "user__email", "role")
    readonly_fields = ("uuid", "created_at", "updated_at")


@admin.register(DocumentProcessingJob)
class DocumentProcessingJobAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "job_type",
        "status",
        "progress_percent",
        "started_at",
        "completed_at",
    )
    list_filter = ("job_type", "status")
    search_fields = ("document__title", "celery_task_id")
    readonly_fields = ("uuid", "created_at", "updated_at")
