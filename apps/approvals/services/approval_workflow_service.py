from django.db import transaction
from django.utils import timezone

from apps.approvals.models import ApprovalComment, ApprovalRequest
from apps.audit.models import AuditLog
from apps.audit.services import AuditLogService
from apps.documents.models import DocumentAccessPolicy
from apps.documents.services import DocumentPermissionService


class ApprovalWorkflowService:
    @classmethod
    def user_can_view(cls, user, approval_request):
        if DocumentPermissionService.is_company_admin(user):
            return True

        if approval_request.requested_by_id == user.id:
            return True

        if approval_request.assigned_to_id == user.id:
            return True

        if approval_request.reviewed_by_id == user.id:
            return True

        return DocumentPermissionService.has_document_access(
            user=user,
            document=approval_request.document,
            required_access=DocumentAccessPolicy.AccessLevel.VIEW,
        )

    @classmethod
    def user_can_review(cls, user, approval_request):
        if approval_request.is_closed:
            return False

        if DocumentPermissionService.is_company_admin(user):
            return True

        if approval_request.assigned_to_id == user.id:
            return True

        return DocumentPermissionService.has_document_access(
            user=user,
            document=approval_request.document,
            required_access=DocumentAccessPolicy.AccessLevel.REVIEW,
        )

    @classmethod
    def user_can_manage(cls, user, approval_request):
        if DocumentPermissionService.is_company_admin(user):
            return True

        if (
            approval_request.requested_by_id == user.id
            and not approval_request.is_closed
        ):
            return True

        return DocumentPermissionService.has_document_access(
            user=user,
            document=approval_request.document,
            required_access=DocumentAccessPolicy.AccessLevel.ADMIN,
        )

    @classmethod
    @transaction.atomic
    def create_approval_request(
        cls,
        user,
        document,
        title,
        description="",
        review_type=ApprovalRequest.ReviewType.DOCUMENT_REVIEW,
        priority=ApprovalRequest.Priority.MEDIUM,
        assigned_to=None,
        due_at=None,
        submitted_payload=None,
        ip_address=None,
        user_agent="",
    ):
        approval = ApprovalRequest.objects.create(
            company=user.company,
            document=document,
            requested_by=user,
            assigned_to=assigned_to,
            review_type=review_type,
            priority=priority,
            title=title,
            description=description,
            due_at=due_at,
            submitted_payload=submitted_payload or {},
            status=ApprovalRequest.Status.PENDING,
        )

        cls.add_system_comment(
            approval_request=approval,
            message="Approval request created.",
        )

        AuditLogService.log(
            company=user.company,
            user=user,
            event_type=AuditLog.EventType.APPROVAL_CREATED,
            severity=AuditLog.Severity.INFO,
            action="Approval request created.",
            resource_type="approval_request",
            resource_uuid=approval.uuid,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={
                "approval_uuid": str(approval.uuid),
                "document_uuid": str(document.uuid),
                "review_type": review_type,
                "assigned_to": assigned_to.id if assigned_to else None,
            },
        )

        return approval

    @classmethod
    @transaction.atomic
    def approve(
        cls,
        user,
        approval_request,
        reviewed_payload=None,
        reviewer_notes="",
        ip_address=None,
        user_agent="",
    ):
        approval_request.status = ApprovalRequest.Status.APPROVED
        approval_request.reviewed_by = user
        approval_request.reviewed_at = timezone.now()
        approval_request.reviewed_payload = (
            reviewed_payload or approval_request.submitted_payload
        )
        approval_request.reviewer_notes = reviewer_notes
        approval_request.save(
            update_fields=[
                "status",
                "reviewed_by",
                "reviewed_at",
                "reviewed_payload",
                "reviewer_notes",
                "updated_at",
            ]
        )

        ApprovalComment.objects.create(
            company=approval_request.company,
            approval_request=approval_request,
            created_by=user,
            comment_type=ApprovalComment.CommentType.DECISION,
            comment=reviewer_notes or "Approved.",
        )

        AuditLogService.log(
            company=user.company,
            user=user,
            event_type=AuditLog.EventType.APPROVAL_APPROVED,
            severity=AuditLog.Severity.INFO,
            action="Approval request approved.",
            resource_type="approval_request",
            resource_uuid=approval_request.uuid,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={
                "approval_uuid": str(approval_request.uuid),
                "document_uuid": str(approval_request.document.uuid),
                "review_type": approval_request.review_type,
            },
        )

        return approval_request

    @classmethod
    @transaction.atomic
    def reject(
        cls,
        user,
        approval_request,
        reviewer_notes,
        ip_address=None,
        user_agent="",
    ):
        approval_request.status = ApprovalRequest.Status.REJECTED
        approval_request.reviewed_by = user
        approval_request.reviewed_at = timezone.now()
        approval_request.reviewer_notes = reviewer_notes
        approval_request.save(
            update_fields=[
                "status",
                "reviewed_by",
                "reviewed_at",
                "reviewer_notes",
                "updated_at",
            ]
        )

        ApprovalComment.objects.create(
            company=approval_request.company,
            approval_request=approval_request,
            created_by=user,
            comment_type=ApprovalComment.CommentType.DECISION,
            comment=reviewer_notes,
        )

        AuditLogService.log(
            company=user.company,
            user=user,
            event_type=AuditLog.EventType.APPROVAL_REJECTED,
            severity=AuditLog.Severity.MEDIUM,
            action="Approval request rejected.",
            resource_type="approval_request",
            resource_uuid=approval_request.uuid,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={
                "approval_uuid": str(approval_request.uuid),
                "document_uuid": str(approval_request.document.uuid),
                "review_type": approval_request.review_type,
            },
        )

        return approval_request

    @classmethod
    @transaction.atomic
    def request_changes(
        cls,
        user,
        approval_request,
        reviewer_notes,
        reviewed_payload=None,
        ip_address=None,
        user_agent="",
    ):
        approval_request.status = ApprovalRequest.Status.CHANGES_REQUESTED
        approval_request.reviewed_by = user
        approval_request.reviewed_at = timezone.now()
        approval_request.reviewer_notes = reviewer_notes
        if reviewed_payload is not None:
            approval_request.reviewed_payload = reviewed_payload

        approval_request.save(
            update_fields=[
                "status",
                "reviewed_by",
                "reviewed_at",
                "reviewer_notes",
                "reviewed_payload",
                "updated_at",
            ]
        )

        ApprovalComment.objects.create(
            company=approval_request.company,
            approval_request=approval_request,
            created_by=user,
            comment_type=ApprovalComment.CommentType.CORRECTION,
            comment=reviewer_notes,
        )

        AuditLogService.log(
            company=user.company,
            user=user,
            event_type=AuditLog.EventType.APPROVAL_CHANGES_REQUESTED,
            severity=AuditLog.Severity.MEDIUM,
            action="Changes requested for approval request.",
            resource_type="approval_request",
            resource_uuid=approval_request.uuid,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={
                "approval_uuid": str(approval_request.uuid),
                "document_uuid": str(approval_request.document.uuid),
                "review_type": approval_request.review_type,
            },
        )

        return approval_request

    @classmethod
    def add_comment(
        cls,
        user,
        approval_request,
        comment,
        comment_type=ApprovalComment.CommentType.GENERAL,
        field_path="",
        old_value="",
        new_value="",
        ip_address=None,
        user_agent="",
    ):
        approval_comment = ApprovalComment.objects.create(
            company=approval_request.company,
            approval_request=approval_request,
            created_by=user,
            comment_type=comment_type,
            comment=comment,
            field_path=field_path,
            old_value=old_value,
            new_value=new_value,
        )

        AuditLogService.log(
            company=user.company,
            user=user,
            event_type=AuditLog.EventType.APPROVAL_COMMENT_ADDED,
            severity=AuditLog.Severity.INFO,
            action="Approval comment added.",
            resource_type="approval_request",
            resource_uuid=approval_request.uuid,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={
                "approval_uuid": str(approval_request.uuid),
                "comment_uuid": str(approval_comment.uuid),
                "comment_type": comment_type,
                "field_path": field_path,
            },
        )

        return approval_comment

    @staticmethod
    def add_system_comment(approval_request, message):
        return ApprovalComment.objects.create(
            company=approval_request.company,
            approval_request=approval_request,
            created_by=None,
            comment_type=ApprovalComment.CommentType.SYSTEM,
            comment=message,
        )
