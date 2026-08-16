from django.db.models import Q
from django.http import FileResponse, Http404
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.documents.models import Document, DocumentProcessingJob
from apps.documents.permissions import IsCompanyDocumentUser
from apps.documents.serializers import (
    DocumentAccessPolicySerializer,
    DocumentCreateSerializer,
    DocumentDetailSerializer,
    DocumentListSerializer,
    InternalAIDocumentListSerializer,
    DocumentProcessingJobSerializer,
    DocumentUpdateSerializer,
)
from apps.documents.services import (
    DocumentPermissionService,
    DocumentProcessingJobService,
    DocumentProcessingQueueService,
)


class InternalAIDocumentPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


def _secure_file_response(document, as_attachment):
    try:
        file_handle = document.file.open("rb")
    except (FileNotFoundError, OSError, ValueError):
        raise Http404("Document file is not available.")

    response = FileResponse(
        file_handle,
        as_attachment=as_attachment,
        filename=document.original_filename or document.title,
        content_type=document.mime_type or "application/octet-stream",
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


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

    @action(detail=False, methods=["get"], url_path="ai-library")
    def ai_library(self, request):
        queryset = (
            DocumentPermissionService.get_accessible_document_queryset(
                user=request.user,
                required_access="view",
                require_ai=True,
                scope=Document.Scope.INTERNAL,
            )
            .filter(
                status=Document.Status.READY,
                is_ai_ready=True,
                is_indexed=True,
            )
            .select_related("department", "uploaded_by")
            .prefetch_related("access_policies")
        )

        search = (request.query_params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search)
                | Q(description__icontains=search)
                | Q(original_filename__icontains=search)
                | Q(department__name__icontains=search)
                | Q(document_type__icontains=search)
            )

        department = request.query_params.get("department")
        document_type = request.query_params.get("document_type")
        sensitivity_label = request.query_params.get("sensitivity_label")

        if department:
            queryset = queryset.filter(department__uuid=department)
        if document_type:
            queryset = queryset.filter(document_type=document_type)
        if sensitivity_label:
            queryset = queryset.filter(sensitivity_label=sensitivity_label)

        ordering = request.query_params.get("ordering", "-updated_at")
        allowed_ordering = {
            "title",
            "-title",
            "created_at",
            "-created_at",
            "updated_at",
            "-updated_at",
            "file_size",
            "-file_size",
        }
        if ordering not in allowed_ordering:
            ordering = "-updated_at"

        queryset = queryset.distinct().order_by(ordering)

        paginator = InternalAIDocumentPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = InternalAIDocumentListSerializer(
            page,
            many=True,
            context={
                **self.get_serializer_context(),
                "view_access_verified": True,
            },
        )
        return paginator.get_paginated_response(serializer.data)

    @action(detail=True, methods=["get"], url_path="preview")
    def preview(self, request, uuid=None):
        document = self.get_object()
        return _secure_file_response(document, as_attachment=False)

    @action(detail=True, methods=["get"], url_path="download")
    def download(self, request, uuid=None):
        document = self.get_object()

        if not DocumentPermissionService.can_download_document(
            user=request.user,
            document=document,
            assume_view_access=True,
        ):
            return Response(
                {"detail": "You do not have permission to download this document."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return _secure_file_response(document, as_attachment=True)

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

        DocumentProcessingQueueService.enqueue(document, reset_state=True)

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
