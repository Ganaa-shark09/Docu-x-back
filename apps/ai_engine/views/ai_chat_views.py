from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_engine.models import AIConversation
from apps.ai_engine.serializers import (
    AIChatRequestSerializer,
    AIConversationDetailSerializer,
    AIConversationListSerializer,
)
from apps.ai_engine.services import RAGChatService
from apps.ai_engine.services.internal_document_scope_service import (
    InternalDocumentScopeError,
)


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

        try:
            result = RAGChatService.chat(
                user=request.user,
                message=serializer.validated_data["message"],
                model_uuid=serializer.validated_data.get("model_uuid"),
                conversation_uuid=serializer.validated_data.get("conversation_uuid"),
                department_uuid=serializer.validated_data.get("department"),
                document_type=serializer.validated_data.get("document_type"),
                sensitivity_label=serializer.validated_data.get("sensitivity_label"),
                document_scope=serializer.validated_data.get("document_scope", "all"),
                document_uuids=serializer.validated_data.get("document_uuids", []),
                top_k=serializer.validated_data.get("top_k", 5),
                ip_address=get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                domain=serializer.validated_data.get("domain"),
                policy_context=serializer.validated_data.get("policy_context"),
                idempotency_key=serializer.validated_data.get("idempotency_key"),
            )
        except InternalDocumentScopeError as exc:
            payload = {
                "detail": exc.detail,
                "code": exc.code,
            }
            if exc.unavailable_document_uuids:
                payload["unavailable_document_uuids"] = exc.unavailable_document_uuids
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)

        assistant_message = result["assistant_message"]
        from apps.ai_engine.services.message_visibility_service import (
            MessageVisibilityService,
        )

        if not MessageVisibilityService.visible(assistant_message, request.user):
            raise PermissionDenied(
                "Document access changed while generating this answer."
            )

        return Response(
            {
                "conversation_uuid": result["conversation"].uuid,
                "message_uuid": assistant_message.uuid,
                "answer": assistant_message.content,
                "sources": result["sources"],
                "document_context": result["document_context"],
                "firewall": result["firewall"],
                "model_provider": assistant_message.model_provider,
                "model_name": assistant_message.model_name,
                "usage": {
                    "prompt_tokens": assistant_message.prompt_tokens,
                    "completion_tokens": assistant_message.completion_tokens,
                    "total_tokens": assistant_message.total_tokens,
                },
                **({"policy": result["policy"]} if "policy" in result else {}),
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


class AIConversationDetailAPIView(generics.RetrieveDestroyAPIView):
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

    def perform_destroy(self, instance):
        # Conversation deletion is intentionally reversible. Messages and
        # attachments remain intact for audit/history and can be restored later.
        instance.status = AIConversation.Status.ARCHIVED
        instance.save(update_fields=["status", "updated_at"])
        instance.soft_delete()


class AIConversationRestoreAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, uuid):
        conversation = get_object_or_404(
            AIConversation,
            uuid=uuid,
            company=request.user.company,
            user=request.user,
            is_deleted=True,
        )
        conversation.restore()
        conversation.status = AIConversation.Status.ACTIVE
        conversation.save(update_fields=["status", "updated_at"])

        return Response(
            AIConversationListSerializer(conversation).data,
            status=status.HTTP_200_OK,
        )
