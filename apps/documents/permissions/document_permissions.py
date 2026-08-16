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
            and getattr(request.user, "company_id", None)
        )

    def has_object_permission(self, request, view, obj):
        required_access = self.ACTION_ACCESS_MAP.get(
            getattr(view, "action", None),
            DocumentAccessPolicy.AccessLevel.VIEW,
        )

        return DocumentPermissionService.has_document_access(
            user=request.user,
            document=obj,
            required_access=required_access,
        )
