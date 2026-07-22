from django.contrib import admin

from apps.approvals.models import ApprovalComment, ApprovalRequest


class ApprovalCommentInline(admin.TabularInline):
    model = ApprovalComment
    extra = 0
    readonly_fields = (
        "uuid",
        "created_at",
        "updated_at",
    )


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "document",
        "company",
        "review_type",
        "status",
        "priority",
        "requested_by",
        "assigned_to",
        "reviewed_by",
        "created_at",
    )
    list_filter = (
        "review_type",
        "status",
        "priority",
        "company",
    )
    search_fields = (
        "title",
        "description",
        "document__title",
        "requested_by__email",
        "assigned_to__email",
    )
    readonly_fields = (
        "uuid",
        "created_at",
        "updated_at",
    )
    inlines = [ApprovalCommentInline]


@admin.register(ApprovalComment)
class ApprovalCommentAdmin(admin.ModelAdmin):
    list_display = (
        "approval_request",
        "comment_type",
        "created_by",
        "field_path",
        "created_at",
    )
    list_filter = (
        "comment_type",
        "company",
    )
    search_fields = (
        "approval_request__title",
        "comment",
        "field_path",
    )
    readonly_fields = (
        "uuid",
        "created_at",
        "updated_at",
    )
