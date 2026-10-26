from django.db import transaction
from rest_framework import permissions, viewsets
from rest_framework.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.accounts.serializers.administration_serializers import (
    ManagedCompanySerializer,
    ManagedUserSerializer,
)
from apps.audit.models import AuditLog
from apps.audit.services import AuditLogService
from apps.tenants.models import Company


def can_manage_company(actor):
    return (
        actor.is_authenticated
        and actor.is_active
        and (
            actor.is_superuser
            or (
                actor.role in {User.Role.COMPANY_ADMIN, User.Role.PLATFORM_ADMIN}
                and actor.company_id
                and not actor.company.is_deleted
                and actor.company.status == Company.Status.ACTIVE
            )
        )
    )


class AdministrationMixin:
    permission_classes = [permissions.IsAuthenticated]

    def log_change(self, company, instance, operation):
        AuditLogService.log(
            company=company,
            user=self.request.user,
            event_type=AuditLog.EventType.SECURITY_EVENT,
            action=f"Administration {operation}.",
            resource_type=instance._meta.model_name,
            resource_uuid=getattr(instance, "uuid", None),
            metadata={"operation": operation, "target_id": instance.pk},
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )

    @transaction.atomic
    def perform_create(self, serializer):
        instance = serializer.save()
        self.log_change(
            instance if isinstance(instance, Company) else instance.company,
            instance,
            "created",
        )

    @transaction.atomic
    def perform_update(self, serializer):
        instance = serializer.save()
        self.log_change(
            instance if isinstance(instance, Company) else instance.company,
            instance,
            "updated",
        )


class ManagedCompanyViewSet(AdministrationMixin, viewsets.ModelViewSet):
    serializer_class = ManagedCompanySerializer
    http_method_names = ["get", "post", "patch", "head", "options"]
    lookup_field = "uuid"

    def get_queryset(self):
        actor = self.request.user
        if not can_manage_company(actor):
            raise PermissionDenied("Company administration is required.")
        qs = Company.objects.filter(is_deleted=False)
        if not actor.is_superuser:
            qs = qs.filter(pk=actor.company_id)
        return qs.order_by("name")

    def perform_create(self, serializer):
        if not self.request.user.is_superuser:
            raise PermissionDenied("Only a superuser may create companies.")
        super().perform_create(serializer)


class ManagedUserViewSet(AdministrationMixin, viewsets.ModelViewSet):
    serializer_class = ManagedUserSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        actor = self.request.user
        if not can_manage_company(actor):
            raise PermissionDenied("Company administration is required.")
        qs = User.objects.filter(is_superuser=False).exclude(
            role=User.Role.PLATFORM_ADMIN
        )
        if actor.is_superuser:
            company_id = self.request.query_params.get("company")
            if not company_id or not company_id.isdecimal():
                return qs.none()
            qs = qs.filter(company_id=company_id)
        else:
            qs = qs.filter(company_id=actor.company_id)
        return qs.order_by("username")

    def perform_create(self, serializer):
        if not can_manage_company(self.request.user):
            raise PermissionDenied("Company administration is required.")
        super().perform_create(serializer)
