from rest_framework import viewsets

from apps.documents.services import DocumentPermissionService
from apps.workspaces.models import Department, DepartmentMembership
from apps.workspaces.permissions import IsCompanyWorkspaceUser
from apps.workspaces.serializers import (
    DepartmentMembershipSerializer,
    DepartmentSerializer,
)


class DepartmentViewSet(viewsets.ModelViewSet):
    serializer_class = DepartmentSerializer
    permission_classes = [IsCompanyWorkspaceUser]
    lookup_field = "uuid"

    def get_queryset(self):
        user = self.request.user

        queryset = Department.objects.filter(
            company=user.company,
            is_deleted=False,
        )

        if DocumentPermissionService.is_company_admin(user):
            return queryset.order_by("name")

        department_ids = DepartmentMembership.objects.filter(
            company=user.company,
            user=user,
            is_active=True,
            is_deleted=False,
        ).values_list("department_id", flat=True)

        return queryset.filter(id__in=department_ids).order_by("name")

    def perform_destroy(self, instance):
        instance.soft_delete()


class DepartmentMembershipViewSet(viewsets.ModelViewSet):
    serializer_class = DepartmentMembershipSerializer
    permission_classes = [IsCompanyWorkspaceUser]
    lookup_field = "uuid"

    def get_queryset(self):
        user = self.request.user

        queryset = DepartmentMembership.objects.filter(
            company=user.company,
            is_deleted=False,
        ).select_related("company", "department", "user")

        if DocumentPermissionService.is_company_admin(user):
            return queryset.order_by("department__name", "user__username")

        manageable_department_ids = DepartmentMembership.objects.filter(
            company=user.company,
            user=user,
            role=DepartmentMembership.Role.DEPARTMENT_ADMIN,
            is_active=True,
            is_deleted=False,
        ).values_list("department_id", flat=True)

        return queryset.filter(department_id__in=manageable_department_ids).order_by(
            "department__name",
            "user__username",
        )

    def perform_destroy(self, instance):
        instance.soft_delete()
