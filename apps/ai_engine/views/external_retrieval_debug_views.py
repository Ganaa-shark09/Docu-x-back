from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_engine.models import AIConversation
from apps.ai_engine.services.external_document_retrieval_service import (
    ExternalDocumentRetrievalService,
)


class ExternalConversationRetrievalDebugAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, conversation_uuid):
        conversation = AIConversation.objects.filter(
            uuid=conversation_uuid,
            company=request.user.company,
            user=request.user,
            scope=AIConversation.Scope.EXTERNAL,
            is_deleted=False,
        ).first()

        if not conversation:
            return Response(
                {"detail": "External conversation not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        query = request.query_params.get("query", "")

        result = ExternalDocumentRetrievalService.retrieve(
            user=request.user,
            conversation=conversation,
            query=query,
            explicit_document_uuids=None,
            top_k=12,
        )

        return Response(result)
