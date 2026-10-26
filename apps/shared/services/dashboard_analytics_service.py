from datetime import datetime, time, timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.ai_engine.models import AIConversation, AIMessage
from apps.approvals.models import ApprovalRequest
from apps.audit.models import AuditLog
from apps.documents.models import Document, DocumentAccessPolicy
from apps.documents.services import DocumentPermissionService
from apps.notifications.models import Notification
from apps.workflows.models import ExtractionRun


class DashboardAnalyticsService:
    @classmethod
    def get_overview(cls, user):
        documents = cls.get_document_metrics(user)
        approvals = cls.get_approval_metrics(user)
        notifications = cls.get_notification_metrics(user)
        extractions = cls.get_extraction_metrics(user)
        rag_usage = cls.get_rag_usage_metrics(user)

        return {
            "documents": documents,
            "approvals": approvals,
            "notifications": notifications,
            "extractions": extractions,
            "rag_usage": rag_usage,
            "generated_at": timezone.now(),
        }

    @classmethod
    def get_document_metrics(cls, user):
        queryset = cls._document_queryset(user)

        total_documents = queryset.count()

        by_status = cls._counts_by_field(queryset, "status")
        by_type = cls._counts_by_field(queryset, "document_type")
        by_sensitivity = cls._counts_by_field(queryset, "sensitivity_label")

        return {
            "total_documents": total_documents,
            "ai_ready_documents": queryset.filter(is_ai_ready=True).count(),
            "indexed_documents": queryset.filter(is_indexed=True).count(),
            "failed_documents": queryset.filter(status=Document.Status.FAILED).count(),
            "processing_documents": queryset.filter(
                status=Document.Status.PROCESSING
            ).count(),
            "by_status": by_status,
            "by_type": by_type,
            "by_sensitivity": by_sensitivity,
            "uploads_by_day": cls._daily_uploads(queryset),
        }

    @staticmethod
    def _daily_uploads(queryset):
        today = timezone.localdate()
        start = today - timedelta(days=29)
        zone = timezone.get_current_timezone()
        lower = timezone.make_aware(datetime.combine(start, time.min), zone)
        upper = timezone.make_aware(
            datetime.combine(today + timedelta(days=1), time.min), zone
        )
        days = {(start + timedelta(days=n)).isoformat(): 0 for n in range(30)}
        rows = (
            queryset.filter(created_at__gte=lower, created_at__lt=upper)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(count=Count("id", distinct=True))
            .order_by("day")
        )
        for row in rows:
            days[row["day"].isoformat()] = row["count"]
        return days

    @classmethod
    def get_approval_metrics(cls, user):
        queryset = cls._approval_queryset(user)

        return {
            "total_approvals": queryset.count(),
            "pending": queryset.filter(status=ApprovalRequest.Status.PENDING).count(),
            "in_review": queryset.filter(
                status=ApprovalRequest.Status.IN_REVIEW
            ).count(),
            "changes_requested": queryset.filter(
                status=ApprovalRequest.Status.CHANGES_REQUESTED
            ).count(),
            "approved": queryset.filter(status=ApprovalRequest.Status.APPROVED).count(),
            "rejected": queryset.filter(status=ApprovalRequest.Status.REJECTED).count(),
            "assigned_to_me": queryset.filter(
                assigned_to=user,
                status__in=[
                    ApprovalRequest.Status.PENDING,
                    ApprovalRequest.Status.IN_REVIEW,
                    ApprovalRequest.Status.CHANGES_REQUESTED,
                ],
            ).count(),
            "requested_by_me": queryset.filter(requested_by=user).count(),
            "by_status": cls._counts_by_field(queryset, "status"),
            "by_review_type": cls._counts_by_field(queryset, "review_type"),
            "by_priority": cls._counts_by_field(queryset, "priority"),
        }

    @classmethod
    def get_notification_metrics(cls, user):
        queryset = Notification.objects.filter(
            company=user.company,
            recipient=user,
            is_deleted=False,
        )

        return {
            "total_notifications": queryset.exclude(
                status=Notification.Status.ARCHIVED
            ).count(),
            "unread_count": queryset.filter(status=Notification.Status.UNREAD).count(),
            "read_count": queryset.filter(status=Notification.Status.READ).count(),
            "archived_count": queryset.filter(
                status=Notification.Status.ARCHIVED
            ).count(),
            "by_type": cls._counts_by_field(queryset, "notification_type"),
            "by_priority": cls._counts_by_field(queryset, "priority"),
        }

    @classmethod
    def get_extraction_metrics(cls, user):
        queryset = cls._extraction_queryset(user)

        return {
            "total_extraction_runs": queryset.count(),
            "pending": queryset.filter(status=ExtractionRun.Status.PENDING).count(),
            "running": queryset.filter(status=ExtractionRun.Status.RUNNING).count(),
            "success": queryset.filter(status=ExtractionRun.Status.SUCCESS).count(),
            "review_required": queryset.filter(
                status=ExtractionRun.Status.REVIEW_REQUIRED
            ).count(),
            "submitted_for_approval": queryset.filter(
                status=ExtractionRun.Status.SUBMITTED_FOR_APPROVAL
            ).count(),
            "approved": queryset.filter(status=ExtractionRun.Status.APPROVED).count(),
            "failed": queryset.filter(status=ExtractionRun.Status.FAILED).count(),
            "average_confidence": cls._average_confidence(queryset),
            "by_status": cls._counts_by_field(queryset, "status"),
            "by_template": cls._counts_by_related_field(queryset, "template__name"),
        }

    @classmethod
    def get_rag_usage_metrics(cls, user):
        conversations = AIConversation.objects.filter(
            company=user.company,
            user=user,
            is_deleted=False,
        )

        assistant_messages = AIMessage.objects.filter(
            conversation__company=user.company,
            conversation__user=user,
            role=AIMessage.Role.ASSISTANT,
            is_deleted=False,
        )

        token_totals = assistant_messages.aggregate(
            prompt_tokens=Sum("prompt_tokens"),
            completion_tokens=Sum("completion_tokens"),
            total_tokens=Sum("total_tokens"),
        )

        return {
            "total_conversations": conversations.count(),
            "total_ai_responses": assistant_messages.count(),
            "total_prompt_tokens": token_totals.get("prompt_tokens") or 0,
            "total_completion_tokens": token_totals.get("completion_tokens") or 0,
            "total_tokens": token_totals.get("total_tokens") or 0,
            "by_provider": cls._counts_by_field(assistant_messages, "model_provider"),
            "by_model": cls._counts_by_field(assistant_messages, "model_name"),
        }

    @classmethod
    def get_recent_activity(cls, user, limit=20):
        limit = max(1, min(int(limit or 20), 100))

        activities = []

        documents = cls._document_queryset(user).order_by("-created_at")[:limit]
        for document in documents:
            activities.append(
                {
                    "type": "document",
                    "title": document.title,
                    "message": f"Document uploaded: {document.title}",
                    "resource_uuid": str(document.uuid),
                    "status": document.status,
                    "created_at": document.created_at,
                    "metadata": {
                        "document_type": document.document_type,
                        "sensitivity_label": document.sensitivity_label,
                    },
                }
            )

        approvals = cls._approval_queryset(user).order_by("-updated_at")[:limit]
        for approval in approvals:
            activities.append(
                {
                    "type": "approval",
                    "title": approval.title,
                    "message": f"Approval {approval.status}: {approval.title}",
                    "resource_uuid": str(approval.uuid),
                    "status": approval.status,
                    "created_at": approval.updated_at,
                    "metadata": {
                        "review_type": approval.review_type,
                        "priority": approval.priority,
                        "document_uuid": str(approval.document.uuid),
                    },
                }
            )

        extractions = cls._extraction_queryset(user).order_by("-updated_at")[:limit]
        for extraction_run in extractions:
            activities.append(
                {
                    "type": "extraction",
                    "title": extraction_run.document.title,
                    "message": (
                        f"Extraction {extraction_run.status}: "
                        f"{extraction_run.document.title}"
                    ),
                    "resource_uuid": str(extraction_run.uuid),
                    "status": extraction_run.status,
                    "created_at": extraction_run.updated_at,
                    "metadata": {
                        "document_uuid": str(extraction_run.document.uuid),
                        "template_name": extraction_run.template.name,
                        "overall_confidence": float(extraction_run.overall_confidence),
                    },
                }
            )

        notifications = (
            Notification.objects.filter(
                company=user.company,
                recipient=user,
                is_deleted=False,
            )
            .exclude(
                status=Notification.Status.ARCHIVED,
            )
            .order_by("-created_at")[:limit]
        )

        for notification in notifications:
            activities.append(
                {
                    "type": "notification",
                    "title": notification.title,
                    "message": notification.message,
                    "resource_uuid": str(notification.uuid),
                    "status": notification.status,
                    "created_at": notification.created_at,
                    "metadata": {
                        "notification_type": notification.notification_type,
                        "priority": notification.priority,
                        "resource_type": notification.resource_type,
                        "related_resource_uuid": (
                            str(notification.resource_uuid)
                            if notification.resource_uuid
                            else None
                        ),
                    },
                }
            )

        audit_logs = cls._audit_queryset(user).order_by("-created_at")[:limit]
        for audit_log in audit_logs:
            activities.append(
                {
                    "type": "audit",
                    "title": audit_log.action,
                    "message": audit_log.action,
                    "resource_uuid": str(audit_log.uuid),
                    "status": audit_log.severity,
                    "created_at": audit_log.created_at,
                    "metadata": {
                        "event_type": audit_log.event_type,
                        "resource_type": audit_log.resource_type,
                        "related_resource_uuid": (
                            str(audit_log.resource_uuid)
                            if audit_log.resource_uuid
                            else None
                        ),
                    },
                }
            )

        activities.sort(key=lambda item: item["created_at"], reverse=True)

        return activities[:limit]

    @staticmethod
    def _counts_by_field(queryset, field_name):
        return {
            item[field_name] or "unknown": item["count"]
            for item in queryset.values(field_name)
            .annotate(count=Count("id"))
            .order_by()
        }

    @staticmethod
    def _counts_by_related_field(queryset, field_name):
        return {
            item[field_name] or "unknown": item["count"]
            for item in queryset.values(field_name)
            .annotate(count=Count("id"))
            .order_by()
        }

    @staticmethod
    def _average_confidence(queryset):
        values = list(
            queryset.exclude(overall_confidence=0).values_list(
                "overall_confidence",
                flat=True,
            )
        )

        if not values:
            return 0

        return round(
            sum(float(value) for value in values) / len(values),
            2,
        )

    @staticmethod
    def _document_queryset(user):
        return DocumentPermissionService.get_accessible_document_queryset(
            user=user,
            required_access=DocumentAccessPolicy.AccessLevel.VIEW,
        )

    @classmethod
    def _approval_queryset(cls, user):
        queryset = ApprovalRequest.objects.filter(
            company=user.company,
            is_deleted=False,
        ).select_related(
            "document",
            "requested_by",
            "assigned_to",
            "reviewed_by",
        )

        if DocumentPermissionService.is_company_admin(user):
            return queryset

        allowed_document_ids = cls._document_queryset(user).values_list("id", flat=True)

        return queryset.filter(
            Q(requested_by=user)
            | Q(assigned_to=user)
            | Q(reviewed_by=user)
            | Q(document_id__in=allowed_document_ids)
        ).distinct()

    @classmethod
    def _extraction_queryset(cls, user):
        queryset = ExtractionRun.objects.filter(
            company=user.company,
            is_deleted=False,
        ).select_related(
            "document",
            "template",
            "requested_by",
        )

        if DocumentPermissionService.is_company_admin(user):
            return queryset

        allowed_document_ids = cls._document_queryset(user).values_list("id", flat=True)

        return queryset.filter(document_id__in=allowed_document_ids)

    @classmethod
    def _audit_queryset(cls, user):
        queryset = AuditLog.objects.filter(
            company=user.company,
            is_deleted=False,
        )

        if DocumentPermissionService.is_company_admin(user):
            return queryset

        return queryset.filter(user=user)
