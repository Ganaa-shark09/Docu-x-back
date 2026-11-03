"""Platform entitlements + company defaults + available picker catalog."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from apps.ai_engine.models import CompanyModelGrant
from apps.ai_engine.serializers.company_model_serializers import CompanyModelGrantSerializer
from apps.ai_engine.services.company_model_access_service import CompanyModelAccessService


class CompanyModelGrantViewSet(viewsets.ModelViewSet):
    """List usable tenant models, platform-only grants, company-admin defaults.

    Superusers: GET/POST/PATCH/DELETE any company's grants, optionally ?company=UUID.
    Others: GET only enabled models for their own company.
    Company admins: POST /<uuid>/set-default/ for an approved tenant model.
    """
    serializer_class = CompanyModelGrantSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "uuid"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method not in ("GET", "HEAD", "OPTIONS") and self.action != "set_default" and not request.user.is_superuser:
            raise PermissionDenied("Only a platform superuser can administer model grants.")

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            qs = CompanyModelGrant.objects.filter(is_deleted=False).select_related("company", "provider_config")
            company_uuid = self.request.query_params.get("company")
            return qs.filter(company__uuid=company_uuid) if company_uuid else qs
        if not user.company_id:
            return CompanyModelGrant.objects.none()
        return CompanyModelAccessService.available(user.company)

    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def _platform_only(self):
        if not self.request.user.is_superuser:
            raise PermissionDenied("Only a platform superuser can grant or revoke models.")

    def perform_create(self, serializer):
        self._platform_only()
        serializer.save()

    def perform_update(self, serializer):
        self._platform_only()
        serializer.save()

    def perform_destroy(self, instance):
        self._platform_only()
        instance.is_enabled = False
        instance.is_default = False
        instance.save(update_fields=["is_enabled", "is_default", "updated_at"])
        instance.soft_delete()

    @action(detail=True, methods=["post"], url_path="set-default")
    def set_default(self, request, uuid=None):
        if not request.user.is_superuser and request.user.role != "company_admin":
            raise PermissionDenied("Only a company admin can change its default model.")
        with transaction.atomic():
            # Scope before object selection, and lock the tenant for concurrent defaults.
            grant = self.get_object()
            # Lock the tenant record so concurrent default changes are serialized.
            type(grant.company).objects.select_for_update().get(pk=grant.company_id)
            selected = CompanyModelAccessService.available(grant.company).filter(pk=grant.pk).first()
            if selected is None:
                raise ValidationError("The selected model is not currently available.")
            selected.is_default = True
            selected.save()
        return Response(self.get_serializer(selected).data, status=status.HTTP_200_OK)
