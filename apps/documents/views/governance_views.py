from django.db import transaction
from rest_framework import permissions, viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import PermissionDenied

from apps.audit.models import AuditLog
from apps.audit.services import AuditLogService
from apps.documents.models import RetentionPolicy, SensitivityPolicy
from apps.documents.serializers.governance_serializers import (
    RetentionPolicySerializer,
    SensitivityPolicySerializer,
)
from apps.documents.services.document_permission_service import (
    DocumentPermissionService,
)


class GovernancePolicyPagination(PageNumberPagination):
    page_size = 50
    max_page_size = 100


class GovernancePolicyPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user.is_authenticated
            and user.is_active
            and user.company_id
            and not user.company.is_deleted
            and user.company.status == "active"
            and DocumentPermissionService.is_company_admin(user)
        )


class GovernancePolicyViewSet(viewsets.ModelViewSet):
    permission_classes = [GovernancePolicyPermission]
    lookup_field = "uuid"
    pagination_class = GovernancePolicyPagination

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.model.objects.none()
        return self.model.objects.filter(
            company=self.request.user.company, is_deleted=False
        ).order_by("pk")

    def log_change(self, instance, operation, before=None):
        after = self.get_serializer(instance).data
        AuditLogService.log(
            company=self.request.user.company,
            user=self.request.user,
            event_type=AuditLog.EventType.GOVERNANCE_POLICY_CHANGED,
            action=f"Governance policy {operation}.",
            resource_type=instance._meta.model_name,
            resource_uuid=instance.uuid,
            ip_address=self.request.META.get("REMOTE_ADDR"),
            user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
            metadata={"operation": operation, "before": before, "after": after},
        )

    @transaction.atomic
    def perform_create(self, serializer):
        instance = serializer.save(company=self.request.user.company)
        self.log_change(instance, "created")

    @transaction.atomic
    def perform_update(self, serializer):
        # Lock policy edits; retention snapshots already attached to documents
        # remain unchanged until an explicit document governance mutation.
        instance = (
            self.get_queryset().select_for_update().get(pk=serializer.instance.pk)
        )
        before = self.get_serializer(instance).data
        serializer.instance = instance
        self.log_change(serializer.save(), "updated", before)

    @transaction.atomic
    def perform_destroy(self, instance):
        if (
            isinstance(instance, SensitivityPolicy)
            and instance.company.documents.filter(
                sensitivity_label=instance.code, is_deleted=False
            ).exists()
        ):
            raise PermissionDenied(
                "A sensitivity policy in use cannot be deleted; deactivate it instead."
            )
        before = self.get_serializer(instance).data
        instance.soft_delete()
        self.log_change(instance, "deleted", before)


class SensitivityPolicyViewSet(GovernancePolicyViewSet):
    model = SensitivityPolicy
    serializer_class = SensitivityPolicySerializer


class RetentionPolicyViewSet(GovernancePolicyViewSet):
    model = RetentionPolicy
    serializer_class = RetentionPolicySerializer
