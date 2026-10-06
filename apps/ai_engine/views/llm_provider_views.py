from rest_framework import viewsets

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

    def perform_destroy(self, instance):
        instance.soft_delete()
