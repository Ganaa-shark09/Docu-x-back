from rest_framework import permissions, viewsets

from apps.ai_engine.models import LLMProviderConfig
from apps.ai_engine.serializers import LLMProviderConfigSerializer


class LLMProviderConfigViewSet(viewsets.ModelViewSet):
    serializer_class = LLMProviderConfigSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "uuid"

    def get_queryset(self):
        return LLMProviderConfig.objects.filter(
            company=self.request.user.company,
            is_deleted=False,
        ).order_by("-is_default", "name")

    def perform_destroy(self, instance):
        instance.soft_delete()
