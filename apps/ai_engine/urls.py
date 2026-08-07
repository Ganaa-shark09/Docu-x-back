from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.ai_engine.views import (
    AIChatAPIView,
    AIConversationDetailAPIView,
    AIConversationListAPIView,
    ExternalChatAPIView,
    ExternalDocumentExtractedTextAPIView,
    ExternalConversationRetrievalDebugAPIView,
    ExternalDocumentViewSet,
    LLMProviderConfigViewSet,
)

router = DefaultRouter()
router.register("llm-providers", LLMProviderConfigViewSet, basename="llm-providers")
router.register(
    "external-documents",
    ExternalDocumentViewSet,
    basename="external-documents",
)

urlpatterns = [
    path("chat/", AIChatAPIView.as_view(), name="ai-chat"),
    path("external-chat/", ExternalChatAPIView.as_view(), name="external-ai-chat"),
    path(
        "external-documents/<uuid:uuid>/extracted-text/",
        ExternalDocumentExtractedTextAPIView.as_view(),
        name="external-document-extracted-text",
    ),
    path(
        "external-conversations/<uuid:conversation_uuid>/retrieval-debug/",
        ExternalConversationRetrievalDebugAPIView.as_view(),
        name="external-conversation-retrieval-debug",
    ),
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
