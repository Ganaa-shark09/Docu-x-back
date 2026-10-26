from rest_framework.permissions import BasePermission

from apps.documents.services import DocumentPermissionService


class ExtractionPermission(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user.is_authenticated
            and user.is_active
            and user.company_id
            and not user.company.is_deleted
            and user.company.status == "active"
        )

    def has_object_permission(self, request, view, obj):
        linked = getattr(obj, "workflow_step_run", None)
        if linked is not None:
            from apps.workflows.permissions.workflow_permissions import (
                WorkflowPermissionService,
            )

            if not WorkflowPermissionService.can(request.user, linked.run, "view"):
                return False
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                from apps.workflows.models import InvoiceRecord
                from apps.workflows.models import ContractReview

                invoice = InvoiceRecord.objects.filter(extraction_run=obj).exists()
                domain_review = (
                    invoice
                    or ContractReview.objects.filter(extraction_run=obj).exists()
                )
                step = (
                    linked.run.step_runs.filter(step=linked.run.current_step).first()
                    if domain_review
                    else linked
                )
                if (
                    view.action != "correct_field"
                    or linked.run.status
                    not in (
                        {"review_required", "exception"}
                        if domain_review
                        else {"exception"}
                    )
                    or not WorkflowPermissionService.can(
                        request.user, linked.run, "review", step
                    )
                ):
                    return False
        action = {
            "rerun": "extract",
            "correct_field": "correct",
            "submit_for_approval": "workflow",
            "destroy": "correct",
            "export": "export",
            "update": "correct",
            "partial_update": "correct",
        }.get(view.action, "view")
        return DocumentPermissionService.can_action(request.user, obj.document, action)


class ExtractionTemplatePermission(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if (
            not user.is_authenticated
            or not user.is_active
            or not user.company_id
            or user.company.is_deleted
            or user.company.status != "active"
        ):
            return False
        return request.method in {
            "GET",
            "HEAD",
            "OPTIONS",
        } or DocumentPermissionService.is_company_admin(user)

    def has_object_permission(self, request, view, obj):
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return obj.is_system or obj.company_id == request.user.company_id
        if obj.is_system:
            if obj.company_id is None and obj.name in {
                "Canonical Invoice Schema v1",
                "Canonical Contract Review Schema v1",
            }:
                return False
            return request.user.is_superuser or request.user.role == "platform_admin"
        return (
            obj.company_id == request.user.company_id
            and DocumentPermissionService.is_company_admin(request.user)
        )
