from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied

from apps.ai_engine.models import LLMProviderConfig
from apps.ai_engine.permissions import IsLLMProviderAdmin
from apps.ai_engine.serializers import LLMProviderConfigSerializer


class LLMProviderConfigViewSet(viewsets.ModelViewSet):
    serializer_class = LLMProviderConfigSerializer
    permission_classes = [IsLLMProviderAdmin]
    lookup_field = "uuid"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return LLMProviderConfig.objects.none()

        user = self.request.user

        if (
            not user
            or not user.is_authenticated
            or not getattr(user, "company_id", None)
        ):
            return LLMProviderConfig.objects.none()

        return LLMProviderConfig.objects.filter(
            company=user.company,
            is_deleted=False,
        ).order_by(
            "-is_default",
            "name",
        )

    def _require_platform_superuser(self):
        # Endpoint URLs, keys, and provider types must not be tenant-editable.
        if not self.request.user.is_superuser:
            raise PermissionDenied("Only a platform superuser may change provider credentials or routing.")

    def perform_create(self, serializer):
        self._require_platform_superuser()
        serializer.save()

    def perform_update(self, serializer):
        self._require_platform_superuser()
        serializer.save()

    def perform_destroy(self, instance):
        self._require_platform_superuser()
        instance.soft_delete()
