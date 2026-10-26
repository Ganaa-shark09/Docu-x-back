from django.db import transaction
from django.utils import timezone

from apps.approvals.models import ApprovalComment, ApprovalRequest
from apps.audit.models import AuditLog
from apps.audit.services import AuditLogService
from apps.documents.models import DocumentAccessPolicy
from apps.documents.services import DocumentPermissionService
from apps.notifications.services import NotificationService
from apps.documents.services.field_permission_service import FieldPermissionService
from rest_framework.exceptions import PermissionDenied, ValidationError


class ApprovalWorkflowService:
    @classmethod
    def user_can_view(cls, user, approval_request):
        step = getattr(approval_request, "workflow_step_run", None)
        if step is not None:
            from apps.workflows.permissions.workflow_permissions import (
                WorkflowPermissionService,
            )

            return WorkflowPermissionService.content_allowed(user, step.run)
        if not DocumentPermissionService.can_action(
            user, approval_request.document, "view"
        ):
            return False
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
        step = getattr(approval_request, "workflow_step_run", None)
        if step is not None:
            from apps.workflows.permissions.workflow_permissions import (
                WorkflowPermissionService,
            )

            return (
                not approval_request.is_closed
                and step.run.status == "approval_required"
                and WorkflowPermissionService.can(user, step.run, "approve", step)
            )
        if not DocumentPermissionService.can_action(
            user, approval_request.document, "correct"
        ) or FieldPermissionService.has_restrictions(
            user, approval_request.document, "view"
        ):
            return False
        if approval_request.is_closed:
            return False

        if DocumentPermissionService.is_company_admin(user):
            return True

        if approval_request.assigned_to_id == user.id:
            return True

        if (
            approval_request.requested_by_id == user.id
            and DocumentPermissionService.has_document_access(
                user=user,
                document=approval_request.document,
                required_access=DocumentAccessPolicy.AccessLevel.REVIEW,
            )
        ):
            return True

        return DocumentPermissionService.has_document_access(
            user=user,
            document=approval_request.document,
            required_access=DocumentAccessPolicy.AccessLevel.REVIEW,
        )

    @classmethod
    def user_can_manage(cls, user, approval_request):
        if getattr(approval_request, "workflow_step_run", None) is not None:
            from apps.workflows.permissions.workflow_permissions import (
                WorkflowPermissionService,
            )

            return WorkflowPermissionService.content_allowed(
                user, approval_request.workflow_step_run.run
            ) and DocumentPermissionService.is_company_admin(user)
        if not DocumentPermissionService.can_action(
            user, approval_request.document, "view"
        ):
            return False
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
        DocumentPermissionService.require_action(user, document, "correct")
        if assigned_to and not DocumentPermissionService.can_action(
            assigned_to, document, "correct"
        ):
            raise ValidationError(
                "Assigned reviewer must have current review permission."
            )
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

        NotificationService.notify_approval_assigned(
            approval_request=approval,
            actor=user,
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
        approval_request = cls._locked_review(user, approval_request)
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

        cls.sync_extraction_run_status(approval_request)

        NotificationService.notify_approval_approved(
            approval_request=approval_request,
            actor=user,
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
        approval_request = cls._locked_review(user, approval_request)
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

        cls.sync_extraction_run_status(approval_request)

        NotificationService.notify_approval_rejected(
            approval_request=approval_request,
            actor=user,
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
        approval_request = cls._locked_review(user, approval_request)
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

        cls.sync_extraction_run_status(approval_request)

        NotificationService.notify_approval_changes_requested(
            approval_request=approval_request,
            actor=user,
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
        if not cls.user_can_view(
            user, approval_request
        ) or FieldPermissionService.has_restrictions(
            user, approval_request.document, "view"
        ):
            raise PermissionDenied(
                "Current document and field permissions are required."
            )
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

        NotificationService.notify_approval_comment_added(
            approval_request=approval_request,
            comment=approval_comment,
            actor=user,
        )

        return approval_comment

    @classmethod
    def sync_extraction_run_status(cls, approval_request):
        from apps.workflows.services.workflow_engine_service import (
            WorkflowEngineService,
        )

        WorkflowEngineService.approval_decided(approval_request)
        if approval_request.review_type != ApprovalRequest.ReviewType.EXTRACTION_REVIEW:
            return

        from apps.workflows.models import ExtractionRun

        extraction_run = ExtractionRun.objects.filter(
            approval_request_uuid=approval_request.uuid,
            company=approval_request.company,
            is_deleted=False,
        ).first()

        if not extraction_run:
            return

        if approval_request.status == ApprovalRequest.Status.APPROVED:
            extraction_run.status = ExtractionRun.Status.APPROVED
        elif approval_request.status == ApprovalRequest.Status.REJECTED:
            extraction_run.status = ExtractionRun.Status.REJECTED
        elif approval_request.status == ApprovalRequest.Status.CHANGES_REQUESTED:
            extraction_run.status = ExtractionRun.Status.REVIEW_REQUIRED
        else:
            return

        extraction_run.save(update_fields=["status", "updated_at"])

    @staticmethod
    def _locked_review(user, approval_request):
        from apps.workflows.models import WorkflowStepRun
        from apps.workflows.services.workflow_engine_service import (
            WorkflowEngineService,
        )

        linked = WorkflowStepRun.objects.filter(approval=approval_request).first()
        if linked:
            WorkflowEngineService.locked(linked.run)
        current = (
            ApprovalRequest.objects.select_for_update()
            .select_related("document")
            .get(pk=approval_request.pk, company=user.company, is_deleted=False)
        )
        if current.is_closed or current.status == ApprovalRequest.Status.DRAFT:
            raise ValidationError("Approval is not open for review.")
        if not ApprovalWorkflowService.user_can_review(user, current):
            raise PermissionDenied("Current document review permission is required.")
        return current

    @staticmethod
    def add_system_comment(approval_request, message):
        return ApprovalComment.objects.create(
            company=approval_request.company,
            approval_request=approval_request,
            created_by=None,
            comment_type=ApprovalComment.CommentType.SYSTEM,
            comment=message,
        )
