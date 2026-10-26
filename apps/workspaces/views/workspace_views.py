from rest_framework import filters, viewsets
from django.db import transaction
from apps.audit.models import AuditLog
from apps.audit.services import AuditLogService

from apps.documents.services import DocumentPermissionService
from apps.workspaces.models import Department, DepartmentMembership
from apps.workspaces.permissions import IsCompanyWorkspaceUser
from apps.workspaces.permissions.workspace_permissions import workspace_company
from apps.workspaces.serializers import (
    DepartmentMembershipSerializer,
    DepartmentSerializer,
)


class WorkspaceMutationMixin:
    def log_mutation(self, instance, operation, before=None):
        safe = {
            key: getattr(instance, key, None)
            for key in ("role", "is_active", "is_deleted", "department_id", "user_id")
        }
        AuditLogService.log(
            company=workspace_company(self.request),
            user=self.request.user,
            event_type=AuditLog.EventType.DOCUMENT_PERMISSION_CHANGED,
            action="Workspace permission changed.",
            resource_type=instance._meta.model_name,
            resource_uuid=instance.uuid,
            metadata={"operation": operation, "before": before, "after": safe},
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )

    @transaction.atomic
    def perform_create(self, serializer):
        self.log_mutation(serializer.save(), "create")

    @transaction.atomic
    def perform_update(self, serializer):
        instance = (
            self.get_queryset().select_for_update().get(pk=serializer.instance.pk)
        )
        self.check_object_permissions(self.request, instance)
        before = {
            key: getattr(instance, key, None)
            for key in ("role", "is_active", "department_id", "user_id")
        }
        serializer.instance = instance
        self.log_mutation(serializer.save(), "update", before)

    @transaction.atomic
    def perform_destroy(self, instance):
        instance = self.get_queryset().select_for_update().get(pk=instance.pk)
        self.check_object_permissions(self.request, instance)
        instance.soft_delete()
        self.log_mutation(instance, "delete")


class DepartmentViewSet(WorkspaceMutationMixin, viewsets.ModelViewSet):
    serializer_class = DepartmentSerializer
    permission_classes = [IsCompanyWorkspaceUser]
    lookup_field = "uuid"
    filter_backends = [filters.SearchFilter]
    search_fields = ["name"]

    def get_queryset(self):
        user = self.request.user

        queryset = Department.objects.filter(
            company=workspace_company(self.request),
            is_deleted=False,
        )
        # Numeric foreign keys are used by workflow commands; detail routes
        # continue to use UUIDs. Resolution stays inside the authorized queryset.
        identifier = self.request.query_params.get("id")
        if identifier:
            if not identifier.isdecimal():
                return queryset.none()
            queryset = queryset.filter(pk=identifier)

        if DocumentPermissionService.is_company_admin(user):
            return queryset.order_by("name")

        department_ids = DepartmentMembership.objects.filter(
            company=workspace_company(self.request),
            user=user,
            is_active=True,
            is_deleted=False,
        ).values_list("department_id", flat=True)

        return queryset.filter(id__in=department_ids).order_by("name")


class DepartmentMembershipViewSet(WorkspaceMutationMixin, viewsets.ModelViewSet):
    serializer_class = DepartmentMembershipSerializer
    permission_classes = [IsCompanyWorkspaceUser]
    lookup_field = "uuid"

    def get_queryset(self):
        user = self.request.user

        queryset = DepartmentMembership.objects.filter(
            company=workspace_company(self.request),
            is_deleted=False,
        ).select_related("company", "department", "user")

        if DocumentPermissionService.is_company_admin(user):
            return queryset.order_by("department__name", "user__username")

        manageable_department_ids = DepartmentMembership.objects.filter(
            company=workspace_company(self.request),
            user=user,
            role=DepartmentMembership.Role.DEPARTMENT_ADMIN,
            is_active=True,
            is_deleted=False,
        ).values_list("department_id", flat=True)

        return queryset.filter(department_id__in=manageable_department_ids).order_by(
            "department__name",
            "user__username",
        )
