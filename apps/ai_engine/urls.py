from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.ai_engine.views import (
    AIChatAPIView,
    AIConversationDetailAPIView,
    AIConversationListAPIView,
    LLMProviderConfigViewSet,
)

router = DefaultRouter()
router.register("llm-providers", LLMProviderConfigViewSet, basename="llm-providers")

urlpatterns = [
    path("chat/", AIChatAPIView.as_view(), name="ai-chat"),
    path(
        "conversations/", AIConversationListAPIView.as_view(), name="ai-conversations"
    ),
    path(
        "conversations/<uuid:uuid>/",
        AIConversationDetailAPIView.as_view(),
        name="ai-conversation-detail",
    ),
]

urlpatterns += router.urls
