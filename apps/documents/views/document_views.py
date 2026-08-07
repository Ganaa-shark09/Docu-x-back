from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.documents.models import Document, DocumentProcessingJob
from apps.documents.permissions import IsCompanyDocumentUser
from apps.documents.serializers import (
    DocumentAccessPolicySerializer,
    DocumentCreateSerializer,
    DocumentDetailSerializer,
    DocumentListSerializer,
    DocumentProcessingJobSerializer,
    DocumentUpdateSerializer,
)
from apps.documents.services import (
    DocumentPermissionService,
    DocumentProcessingJobService,
)
from apps.documents.tasks import process_document_pipeline


class DocumentViewSet(viewsets.ModelViewSet):
    permission_classes = [IsCompanyDocumentUser]
    lookup_field = "uuid"
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "title",
        "description",
        "original_filename",
    ]
    ordering_fields = [
        "created_at",
        "updated_at",
        "title",
        "file_size",
        "document_type",
        "status",
        "sensitivity_label",
    ]
    ordering = ["-created_at"]

    def get_queryset(self):
        user = self.request.user

        queryset = (
            DocumentPermissionService.get_accessible_document_queryset(
                user=user,
                required_access="view",
                scope=self.request.query_params.get("scope", Document.Scope.INTERNAL),
            )
            .select_related(
                "company",
                "department",
                "uploaded_by",
            )
            .prefetch_related(
                "versions",
                "access_policies",
                "processing_jobs",
            )
        )

        department = self.request.query_params.get("department")
        document_type = self.request.query_params.get("document_type")
        status_value = self.request.query_params.get("status")
        sensitivity_label = self.request.query_params.get("sensitivity_label")
        is_ai_ready = self.request.query_params.get("is_ai_ready")

        if department:
            queryset = queryset.filter(department__uuid=department)

        if document_type:
            queryset = queryset.filter(document_type=document_type)

        if status_value:
            queryset = queryset.filter(status=status_value)

        if sensitivity_label:
            queryset = queryset.filter(sensitivity_label=sensitivity_label)

        if is_ai_ready is not None:
            normalized = is_ai_ready.lower()
            if normalized in ["true", "1", "yes"]:
                queryset = queryset.filter(is_ai_ready=True)
            elif normalized in ["false", "0", "no"]:
                queryset = queryset.filter(is_ai_ready=False)

        return queryset.distinct()

    def get_serializer_class(self):
        if self.action == "create":
            return DocumentCreateSerializer

        if self.action in ["update", "partial_update"]:
            return DocumentUpdateSerializer

        if self.action == "list":
            return DocumentListSerializer

        return DocumentDetailSerializer

    def get_parser_classes(self):
        if self.action == "create":
            return [MultiPartParser, FormParser]

        return super().get_parser_classes()

    def perform_create(self, serializer):
        serializer.save()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = serializer.save()

        response_serializer = DocumentDetailSerializer(
            document,
            context=self.get_serializer_context(),
        )

        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED,
        )

    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=True, methods=["get"], url_path="processing-jobs")
    def processing_jobs(self, request, uuid=None):
        document = self.get_object()
        jobs = document.processing_jobs.all().order_by("created_at")

        serializer = DocumentProcessingJobSerializer(
            jobs,
            many=True,
            context=self.get_serializer_context(),
        )

        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="retry-processing")
    def retry_processing(self, request, uuid=None):
        document = self.get_object()

        document.processing_jobs.filter(
            status__in=[
                DocumentProcessingJob.Status.PENDING,
                DocumentProcessingJob.Status.RUNNING,
                DocumentProcessingJob.Status.FAILED,
            ]
        ).delete()

        DocumentProcessingJobService.create_default_jobs_for_document(document)

        document.status = Document.Status.UPLOADED
        document.processing_error = ""
        document.is_ocr_completed = False
        document.is_ai_ready = False
        document.is_indexed = False
        document.save(
            update_fields=[
                "status",
                "processing_error",
                "is_ocr_completed",
                "is_ai_ready",
                "is_indexed",
                "updated_at",
            ]
        )

        process_document_pipeline.delay(str(document.uuid))

        return Response(
            {
                "detail": "Document processing retry started.",
                "document_uuid": str(document.uuid),
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=["get", "post"], url_path="access-policies")
    def access_policies(self, request, uuid=None):
        document = self.get_object()

        if not DocumentPermissionService.has_document_access(
            user=request.user,
            document=document,
            required_access="admin",
        ):
            return Response(
                {"detail": "Admin access is required to manage document policies."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if request.method == "GET":
            serializer = DocumentAccessPolicySerializer(
                document.access_policies.filter(is_deleted=False),
                many=True,
                context=self.get_serializer_context(),
            )
            return Response(serializer.data)

        serializer = DocumentAccessPolicySerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(document=document)

        return Response(serializer.data, status=status.HTTP_201_CREATED)
