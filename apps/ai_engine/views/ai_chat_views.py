from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_engine.models import AIConversation
from apps.ai_engine.serializers import (
    AIChatRequestSerializer,
    AIConversationDetailSerializer,
    AIConversationListSerializer,
)
from apps.ai_engine.services import RAGChatService


def get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")

    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    return request.META.get("REMOTE_ADDR")


class AIChatAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not getattr(request.user, "company_id", None):
            return Response(
                {"detail": "User is not assigned to a company."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = AIChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = RAGChatService.chat(
            user=request.user,
            message=serializer.validated_data["message"],
            conversation_uuid=serializer.validated_data.get("conversation_uuid"),
            department_uuid=serializer.validated_data.get("department"),
            document_type=serializer.validated_data.get("document_type"),
            sensitivity_label=serializer.validated_data.get("sensitivity_label"),
            top_k=serializer.validated_data.get("top_k", 5),
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

        assistant_message = result["assistant_message"]

        return Response(
            {
                "conversation_uuid": result["conversation"].uuid,
                "message_uuid": assistant_message.uuid,
                "answer": assistant_message.content,
                "sources": result["sources"],
                "firewall": result["firewall"],
                "model_provider": assistant_message.model_provider,
                "model_name": assistant_message.model_name,
                "usage": {
                    "prompt_tokens": assistant_message.prompt_tokens,
                    "completion_tokens": assistant_message.completion_tokens,
                    "total_tokens": assistant_message.total_tokens,
                },
            },
            status=status.HTTP_200_OK,
        )


class AIConversationListAPIView(generics.ListAPIView):
    serializer_class = AIConversationListSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user

        scope = self.request.query_params.get(
            "scope",
            AIConversation.Scope.INTERNAL,
        )

        if scope not in [
            AIConversation.Scope.INTERNAL,
            AIConversation.Scope.EXTERNAL,
        ]:
            scope = AIConversation.Scope.INTERNAL

        return AIConversation.objects.filter(
            company=user.company,
            user=user,
            scope=scope,
            is_deleted=False,
        ).order_by("-updated_at")


class AIConversationDetailAPIView(generics.RetrieveAPIView):
    serializer_class = AIConversationDetailSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "uuid"

    def get_queryset(self):
        return (
            AIConversation.objects.filter(
                company=self.request.user.company,
                user=self.request.user,
                is_deleted=False,
            )
            .select_related("department", "company", "user")
            .prefetch_related("messages")
        )
