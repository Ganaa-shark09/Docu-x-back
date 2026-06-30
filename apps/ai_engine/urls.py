from django.urls import path

from apps.ai_engine.views import (
    AIChatAPIView,
    AIConversationDetailAPIView,
    AIConversationListAPIView,
)

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
