from django.db import transaction
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from rest_framework.views import APIView

from apps.ai_engine.models import AIConversation
from apps.ai_engine.services import ExternalChatService
from apps.ai_engine.services.external_document_readiness_service import (
    ExternalDocumentReadinessService,
)
from apps.ai_engine.services.external_semantic_orchestrator_service import (
    ExternalSemanticOrchestratorService,
)
from apps.documents.models import Document
from apps.documents.permissions.document_permissions import IsCompanyDocumentUser
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

        message = str(request.data.get("message") or "").strip()
        if not message:
            return Response(
                {"message": "This field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        conversation_uuid = request.data.get("conversation_uuid") or None
        document_uuids = ExternalDocumentReadinessService.normalize_uuids(
            request.data.get("document_uuids")
        )
        document_uuids = document_uuids or None

        requested_scope_mode = (
            str(request.data.get("document_scope_mode") or "").strip().lower()
        )
        if requested_scope_mode not in {"", "selected", "conversation"}:
            return Response(
                {
                    "document_scope_mode": (
                        "Must be either 'selected' or 'conversation' when supplied."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            top_k = int(request.data.get("top_k", 8))
        except (TypeError, ValueError):
            return Response(
                {"top_k": "Must be an integer between 1 and 20."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if top_k < 1 or top_k > 20:
            return Response(
                {"top_k": "Must be an integer between 1 and 20."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        use_web_value = request.data.get("use_web", True)
        use_web = (
            use_web_value
            if isinstance(use_web_value, bool)
            else str(use_web_value).strip().lower() in {"true", "1", "yes"}
        )

        routing_plan = None
        document_descriptors = []
        document_scope_mode = "none"

        # The frontend may send either the specifically selected documents or all
        # documents currently attached to the conversation. Document readiness
        # must therefore not block an unrelated general query. We always validate
        # ownership/conversation membership first, but only gate on processing or
        # failure when semantic routing says the request actually requires document
        # content. The WebSocket normally prevents this race; this remains the
        # authoritative backend safety net.
        if document_uuids:
            readiness = ExternalDocumentReadinessService.inspect(
                user=request.user,
                document_uuids=document_uuids,
                conversation_uuid=conversation_uuid,
                scope_mode_hint=requested_scope_mode or None,
            )
            document_descriptors = readiness["documents"]
            document_scope_mode = readiness.get("document_scope_mode", "selected")

            if readiness["missing_document_uuids"]:
                return Response(
                    {
                        "status": "attachments_invalid",
                        "detail": "One or more attached files are unavailable for this conversation.",
                        **readiness,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if readiness["has_processing"] or readiness["has_failed"]:
                # Selected scope is an explicit instruction to use these files, so
                # readiness can be decided immediately without spending a model call
                # asking whether the documents are needed. This also makes the
                # upload+query path return HTTP 202 quickly while processing runs.
                if document_scope_mode == "selected":
                    routing_plan = {
                        "action": ExternalSemanticOrchestratorService.ACTION_DOCUMENT,
                        "document_dependency": (
                            ExternalSemanticOrchestratorService.DOCUMENT_DEPENDENCY_REQUIRED
                        ),
                        "document_task": "qa",
                        "document_query": message,
                        "web_query": "",
                        "answer_tier": "quality",
                        "task_domain": "general",
                        "answer_profile": "general",
                        "reasoning_mode": "none",
                        "quality_focus": [],
                        "quality_requirements": [],
                        "provider": "system",
                        "model": "selected_document_readiness_gate",
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "metadata": {"selected_document_scope_forced": True},
                    }
                else:
                    conversation = None
                    if conversation_uuid:
                        conversation = AIConversation.objects.filter(
                            uuid=conversation_uuid,
                            company=request.user.company,
                            user=request.user,
                            scope=AIConversation.Scope.EXTERNAL,
                            is_deleted=False,
                        ).first()

                    routing_plan = ExternalSemanticOrchestratorService.plan_resilient(
                        message=message,
                        conversation=conversation,
                        has_attached_documents=bool(document_uuids),
                        has_explicit_documents=False,
                        use_web=use_web,
                        document_descriptors=document_descriptors,
                        document_scope_mode=document_scope_mode,
                    )

                requires_documents = (
                    routing_plan.get("document_dependency")
                    == ExternalSemanticOrchestratorService.DOCUMENT_DEPENDENCY_REQUIRED
                    or routing_plan.get("action")
                    in {
                        ExternalSemanticOrchestratorService.ACTION_DOCUMENT,
                        ExternalSemanticOrchestratorService.ACTION_DOCUMENT_WEB,
                    }
                )

                if requires_documents and readiness["has_failed"]:
                    return Response(
                        {
                            "status": "attachment_processing_failed",
                            "detail": "One or more documents required for this query failed processing.",
                            "failed_document_uuids": [
                                item["uuid"] for item in readiness["failed_documents"]
                            ],
                            **readiness,
                        },
                        status=status.HTTP_409_CONFLICT,
                    )

                if requires_documents and (
                    readiness["has_processing"] or not readiness["all_ready"]
                ):
                    pending_uuids = [
                        item["uuid"] for item in readiness["processing_documents"]
                    ]
                    return Response(
                        {
                            "status": "attachments_processing",
                            "detail": "The documents required for this query are still being processed.",
                            "wait_for_document_uuids": pending_uuids,
                            "pending_document_uuids": pending_uuids,
                            "retry_after_ms": 2000,
                            **readiness,
                        },
                        status=status.HTTP_202_ACCEPTED,
                    )

        result = ExternalChatService.chat(
            user=request.user,
            message=message,
            conversation_uuid=conversation_uuid,
            top_k=top_k,
            use_web=use_web,
            document_uuids=document_uuids,
            routing_plan=routing_plan,
            document_descriptors=document_descriptors,
            document_scope_mode=document_scope_mode,
        )

        assistant_message = result["assistant_message"]
        from apps.ai_engine.services.message_visibility_service import (
            MessageVisibilityService,
        )

        if not MessageVisibilityService.visible(assistant_message, request.user):
            raise PermissionDenied(
                "Document access changed while generating this answer."
            )
        routing_context = (result["user_message"].metadata or {}).get("routing", {})

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
                    "requested_document_uuids": document_uuids or [],
                    "document_scope_mode": document_scope_mode,
                    "routing": routing_context,
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
    permission_classes = [IsCompanyDocumentUser]
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

    @action(
        detail=False,
        methods=["get"],
        url_path="processing-status",
    )
    def processing_status(self, request):
        document_uuids = request.query_params.getlist("document_uuid")
        if not document_uuids:
            document_uuids = request.query_params.get("document_uuids") or ""

        normalized = ExternalDocumentReadinessService.normalize_uuids(document_uuids)
        if not normalized:
            return Response(
                {"detail": "At least one document UUID is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = ExternalDocumentReadinessService.inspect(
            user=request.user,
            document_uuids=normalized,
            conversation_uuid=request.query_params.get("conversation_uuid") or None,
        )
        return Response(result, status=status.HTTP_200_OK)

    def perform_destroy(self, instance):
        from apps.documents.services.document_governance_service import (
            DocumentGovernanceService,
        )

        DocumentGovernanceService.delete(
            self.request.user, instance, request=self.request
        )
