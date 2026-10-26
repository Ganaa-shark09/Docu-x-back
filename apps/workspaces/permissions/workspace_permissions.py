from rest_framework.permissions import BasePermission

from apps.documents.services import DocumentPermissionService
from apps.workspaces.models import DepartmentMembership
from apps.workspaces.models import Department
from apps.tenants.models import Company


def workspace_company(request):
    user = request.user
    if not user or not user.is_authenticated or not user.is_active:
        return None
    if user.is_superuser and request.query_params.get("company"):
        identifier = request.query_params["company"]
        if not identifier.isdecimal():
            return None
        return Company.objects.filter(
            pk=identifier, is_deleted=False, status="active"
        ).first()
    if not user.company_id:
        return None
    company = user.company
    return company if not company.is_deleted and company.status == "active" else None


class IsCompanyWorkspaceUser(BasePermission):
    message = "You do not have access to this workspace resource."

    def has_permission(self, request, view):
        company = workspace_company(request)
        if company is None:
            return False
        if view.action == "create":
            if view.basename == "departments":
                return DocumentPermissionService.is_company_admin(request.user)
            department = Department.objects.filter(
                pk=request.data.get("department"),
                company=company,
                is_deleted=False,
                is_active=True,
            ).first()
            return department is not None and (
                request.user.is_superuser or self.can_manage(request.user, department)
            )
        return True

    @staticmethod
    def can_manage(user, department):
        if department.company_id != user.company_id:
            return False
        return (
            DocumentPermissionService.is_company_admin(user)
            or DepartmentMembership.objects.filter(
                company=user.company,
                user=user,
                department=department,
                role=DepartmentMembership.Role.DEPARTMENT_ADMIN,
                is_active=True,
                is_deleted=False,
            ).exists()
        )

    def has_object_permission(self, request, view, obj):
        company = workspace_company(request)
        if company is None or getattr(obj, "company_id", None) != company.pk:
            return False
        if DocumentPermissionService.is_company_admin(request.user):
            return True

        department = getattr(obj, "department", obj)

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            return self.can_manage(request.user, department)

        return DepartmentMembership.objects.filter(
            company=request.user.company,
            user=request.user,
            department=department,
            is_active=True,
            is_deleted=False,
        ).exists()
