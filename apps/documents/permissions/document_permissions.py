from rest_framework.permissions import BasePermission

from apps.documents.models import DocumentAccessPolicy
from apps.documents.services import DocumentPermissionService


class IsCompanyDocumentUser(BasePermission):
    message = "You do not have permission to access this document."

    ACTION_ACCESS_MAP = {
        "list": DocumentAccessPolicy.AccessLevel.VIEW,
        "retrieve": DocumentAccessPolicy.AccessLevel.VIEW,
        "create": DocumentAccessPolicy.AccessLevel.EDIT,
        "update": DocumentAccessPolicy.AccessLevel.EDIT,
        "partial_update": DocumentAccessPolicy.AccessLevel.EDIT,
        "destroy": DocumentAccessPolicy.AccessLevel.ADMIN,
        "ai_library": DocumentAccessPolicy.AccessLevel.VIEW,
        "preview": DocumentAccessPolicy.AccessLevel.VIEW,
        "download": DocumentAccessPolicy.AccessLevel.VIEW,
        "processing_jobs": DocumentAccessPolicy.AccessLevel.VIEW,
        "retry_processing": DocumentAccessPolicy.AccessLevel.EDIT,
        "access_policies": DocumentAccessPolicy.AccessLevel.ADMIN,
    }

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_active
            and getattr(request.user, "company_id", None)
            and not request.user.company.is_deleted
            and request.user.company.status == "active"
        )

    def has_object_permission(self, request, view, obj):
        actions = {
            "preview": "download",
            "download": "download",
            "destroy": "delete",
            "update": "edit",
            "partial_update": "edit",
            "access_policies": "administer",
            "field_policies": "administer",
            "remove_field_policy": "administer",
            "remove_access_policy": "administer",
            "retry_processing": "edit",
            "governance": "administer"
            if request.method != "GET"
            else "governance_read",
        }
        if view.action in actions:
            return DocumentPermissionService.can_action(
                request.user, obj, actions[view.action]
            )
        required_access = self.ACTION_ACCESS_MAP.get(
            getattr(view, "action", None),
            DocumentAccessPolicy.AccessLevel.VIEW,
        )

        return (
            DocumentPermissionService.can_action(request.user, obj, "view")
            if required_access == "view"
            else DocumentPermissionService.has_document_access(
                request.user, obj, required_access=required_access
            )
        )
