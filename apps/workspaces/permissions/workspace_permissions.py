from rest_framework.permissions import BasePermission

from apps.documents.services import DocumentPermissionService
from apps.workspaces.models import DepartmentMembership


class IsCompanyWorkspaceUser(BasePermission):
    message = "You do not have access to this workspace resource."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "company_id", None)
        )

    def has_object_permission(self, request, view, obj):
        if DocumentPermissionService.is_company_admin(request.user):
            return True

        department = getattr(obj, "department", obj)

        return DepartmentMembership.objects.filter(
            company=request.user.company,
            user=request.user,
            department=department,
            is_active=True,
            is_deleted=False,
        ).exists()
