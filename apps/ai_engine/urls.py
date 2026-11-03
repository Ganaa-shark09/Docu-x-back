from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.ai_engine.views import (
    AIChatAPIView,
    AIConversationDetailAPIView,
    AIConversationListAPIView,
    AIConversationRestoreAPIView,
    ExternalChatAPIView,
    ExternalDocumentExtractedTextAPIView,
    ExternalConversationRetrievalDebugAPIView,
    ExternalDocumentViewSet,
    LLMProviderConfigViewSet,
)

from apps.ai_engine.views.company_model_views import CompanyModelGrantViewSet
from apps.ai_engine.views.platform_model_administration import (
    PlatformCompanyListAPIView, PlatformCompanyDetailAPIView,
    PlatformProviderListAPIView, PlatformProviderDetailAPIView,
)

router = DefaultRouter()
router.register("company-models", CompanyModelGrantViewSet, basename="company-models")
router.register("llm-providers", LLMProviderConfigViewSet, basename="llm-providers")
router.register(
    "external-documents",
    ExternalDocumentViewSet,
    basename="external-documents",
)

urlpatterns = [
    path("platform/companies/", PlatformCompanyListAPIView.as_view(), name="platform-ai-companies"),
    path("platform/companies/<uuid:uuid>/", PlatformCompanyDetailAPIView.as_view(), name="platform-ai-company"),
    path("platform/providers/", PlatformProviderListAPIView.as_view(), name="platform-ai-providers"),
    path("platform/providers/<uuid:uuid>/", PlatformProviderDetailAPIView.as_view(), name="platform-ai-provider"),
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
    path(
        "conversations/<uuid:uuid>/restore/",
        AIConversationRestoreAPIView.as_view(),
        name="ai-conversation-restore",
    ),
]

urlpatterns += router.urls
