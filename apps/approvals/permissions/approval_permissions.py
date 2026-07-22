from rest_framework.permissions import BasePermission

from apps.approvals.services import ApprovalWorkflowService


class IsApprovalWorkflowUser(BasePermission):
    message = "You do not have access to this approval request."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "company_id", None)
        )

    def has_object_permission(self, request, view, obj):
        if getattr(view, "action", None) in [
            "approve",
            "reject",
            "request_changes",
        ]:
            return ApprovalWorkflowService.user_can_review(request.user, obj)

        if getattr(view, "action", None) in [
            "update",
            "partial_update",
            "destroy",
        ]:
            return ApprovalWorkflowService.user_can_manage(request.user, obj)

        return ApprovalWorkflowService.user_can_view(request.user, obj)
