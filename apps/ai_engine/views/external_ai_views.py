from django.db import transaction
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_engine.services import ExternalChatService
from apps.documents.models import Document
from apps.documents.serializers.external_document_serializers import (
    ExternalDocumentSerializer,
    ExternalDocumentUploadSerializer,
)
from apps.documents.services.external_document_upload_service import (
    ExternalDocumentUploadService,
)


class ExternalChatAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser]

    def post(self, request):
        if not getattr(request.user, "company_id", None):
            return Response(
                {"detail": "User is not assigned to a company."},
                status=status.HTTP_403_FORBIDDEN,
            )

        message = request.data.get("message")
        if not message:
            return Response(
                {"message": "This field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = ExternalChatService.chat(
            user=request.user,
            message=message,
            conversation_uuid=request.data.get("conversation_uuid"),
            top_k=int(request.data.get("top_k", 8)),
            use_web=bool(request.data.get("use_web", True)),
            document_uuids=request.data.get("document_uuids") or None,
        )

        assistant_message = result["assistant_message"]

        return Response(
            {
                "conversation_uuid": result["conversation"].uuid,
                "message_uuid": assistant_message.uuid,
                "answer": assistant_message.content,
                "sources": result["sources"],
                "model_provider": assistant_message.model_provider,
                "model_name": assistant_message.model_name,
                "usage": {
                    "prompt_tokens": assistant_message.prompt_tokens,
                    "completion_tokens": assistant_message.completion_tokens,
                    "total_tokens": assistant_message.total_tokens,
                },
                "external_context": {
                    "intent": result.get("intent"),
                    "external_chunk_count": len(result["external_chunks"]),
                    "web_result_count": len(result["web_results"]),
                    "processing_documents": result.get("processing_documents", []),
                    "failed_documents": result.get("failed_documents", []),
                    "search_warnings": result.get("search_warnings", []),
                },
            },
            status=status.HTTP_200_OK,
        )


class ExternalDocumentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "uuid"
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        user = self.request.user

        queryset = Document.objects.filter(
            company=user.company,
            scope=Document.Scope.EXTERNAL,
            is_deleted=False,
        ).select_related("company", "uploaded_by")

        conversation_uuid = self.request.query_params.get("conversation_uuid")

        if conversation_uuid:
            queryset = queryset.filter(
                ai_conversation_attachments__conversation__uuid=conversation_uuid,
                ai_conversation_attachments__is_deleted=False,
            )

        if user.is_superuser or getattr(user, "role", None) in [
            "platform_admin",
            "company_admin",
        ]:
            return queryset.distinct().order_by("-created_at")

        return queryset.filter(uploaded_by=user).distinct().order_by("-created_at")

    def get_serializer_class(self):
        if self.action == "create":
            return ExternalDocumentUploadSerializer

        return ExternalDocumentSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        document = serializer.save()

        response_serializer = ExternalDocumentSerializer(
            document,
            context=self.get_serializer_context(),
        )

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @action(
        detail=False,
        methods=["post"],
        url_path="bulk-upload",
        parser_classes=[MultiPartParser, FormParser],
    )
    @transaction.atomic
    def bulk_upload(self, request):
        if not getattr(request.user, "company_id", None):
            return Response(
                {"detail": "User is not assigned to a company."},
                status=status.HTTP_403_FORBIDDEN,
            )

        files = request.FILES.getlist("files")

        # Support fallback if frontend sends repeated "file" keys.
        if not files:
            files = request.FILES.getlist("file")

        if not files:
            return Response(
                {"files": "At least one file is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        conversation_uuid = request.data.get("conversation_uuid") or None
        common_title = request.data.get("title") or ""
        description = request.data.get("description") or ""
        document_type = request.data.get("document_type") or Document.DocumentType.OTHER
        sensitivity_label = (
            request.data.get("sensitivity_label") or Document.Sensitivity.INTERNAL
        )

        conversation = (
            ExternalDocumentUploadService.get_or_create_external_conversation(
                user=request.user,
                conversation_uuid=conversation_uuid,
                title=common_title or files[0].name,
            )
        )

        uploaded_documents = []

        for uploaded_file in files:
            document_title = (
                common_title if len(files) == 1 and common_title else uploaded_file.name
            )

            document, conversation = (
                ExternalDocumentUploadService.upload_external_document(
                    user=request.user,
                    uploaded_file=uploaded_file,
                    conversation=conversation,
                    title=document_title,
                    description=description,
                    document_type=document_type,
                    sensitivity_label=sensitivity_label,
                )
            )

            document._external_conversation_uuid = str(conversation.uuid)
            uploaded_documents.append(document)

        serializer = ExternalDocumentSerializer(
            uploaded_documents,
            many=True,
            context=self.get_serializer_context(),
        )

        return Response(
            {
                "conversation_uuid": str(conversation.uuid),
                "uploaded_count": len(uploaded_documents),
                "documents": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )

    def perform_destroy(self, instance):
        instance.soft_delete()
