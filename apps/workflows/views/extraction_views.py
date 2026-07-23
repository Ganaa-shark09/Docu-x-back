from django.db.models import Q
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.approvals.serializers import ApprovalRequestDetailSerializer
from apps.documents.services import DocumentPermissionService
from apps.workflows.models import (
    ExtractionResultField,
    ExtractionRun,
    ExtractionTemplate,
)
from apps.workflows.serializers import (
    ExtractionFieldCorrectionSerializer,
    ExtractionResultFieldSerializer,
    ExtractionRunCreateSerializer,
    ExtractionRunDetailSerializer,
    ExtractionRunListSerializer,
    ExtractionTemplateFieldSerializer,
    ExtractionTemplateSerializer,
    SubmitExtractionForApprovalSerializer,
)
from apps.workflows.services import ExtractionApprovalService, ExtractionExportService


def get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class ExtractionTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = ExtractionTemplateSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "uuid"
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "description", "template_type"]
    ordering_fields = ["name", "template_type", "created_at"]
    ordering = ["name"]

    def get_queryset(self):
        user = self.request.user

        return (
            ExtractionTemplate.objects.filter(
                is_deleted=False,
                is_active=True,
            )
            .filter(Q(company=user.company) | Q(is_system=True))
            .order_by("-is_system", "name")
        )

    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=True, methods=["post"], url_path="fields")
    def add_field(self, request, uuid=None):
        template = self.get_object()

        serializer = ExtractionTemplateFieldSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(template=template)

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ExtractionRunViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "uuid"

    def get_queryset(self):
        user = self.request.user

        queryset = (
            ExtractionRun.objects.filter(
                company=user.company,
                is_deleted=False,
            )
            .select_related("document", "template", "requested_by")
            .prefetch_related("result_fields")
        )

        if DocumentPermissionService.is_company_admin(user):
            return queryset.order_by("-created_at")

        allowed_document_ids = (
            DocumentPermissionService.get_accessible_document_queryset(
                user=user,
                required_access="view",
                require_ai=True,
            ).values_list("id", flat=True)
        )

        return queryset.filter(document_id__in=allowed_document_ids).order_by(
            "-created_at"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return ExtractionRunCreateSerializer

        if self.action == "list":
            return ExtractionRunListSerializer

        return ExtractionRunDetailSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        extraction_run = serializer.save()

        response_serializer = ExtractionRunDetailSerializer(
            extraction_run,
            context=self.get_serializer_context(),
        )

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=True, methods=["post"], url_path="rerun")
    def rerun(self, request, uuid=None):
        extraction_run = self.get_object()

        if not DocumentPermissionService.has_document_access(
            user=request.user,
            document=extraction_run.document,
            required_access="review",
            require_ai=True,
        ):
            return Response(
                {"detail": "Review access is required to rerun extraction."},
                status=status.HTTP_403_FORBIDDEN,
            )

        extraction_run.status = ExtractionRun.Status.PENDING
        extraction_run.error_message = ""
        extraction_run.raw_output = {}
        extraction_run.overall_confidence = 0
        extraction_run.started_at = None
        extraction_run.completed_at = None
        extraction_run.save(
            update_fields=[
                "status",
                "error_message",
                "raw_output",
                "overall_confidence",
                "started_at",
                "completed_at",
                "updated_at",
            ]
        )

        extraction_run.result_fields.all().delete()

        from apps.workflows.tasks import run_structured_extraction_task

        run_structured_extraction_task.delay(str(extraction_run.uuid))

        return Response(
            {
                "detail": "Extraction rerun started.",
                "extraction_run_uuid": str(extraction_run.uuid),
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=["get"], url_path="export")
    def export(self, request, uuid=None):
        extraction_run = self.get_object()

        if not DocumentPermissionService.has_document_access(
            user=request.user,
            document=extraction_run.document,
            required_access="view",
            require_ai=True,
        ):
            return Response(
                {"detail": "You do not have access to export this extraction."},
                status=status.HTTP_403_FORBIDDEN,
            )

        export_format = request.query_params.get(
            "export_format",
            request.query_params.get("file_type", "json"),
        )

        return ExtractionExportService.export(
            user=request.user,
            extraction_run=extraction_run,
            export_format=export_format,
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

    @action(detail=True, methods=["post"], url_path="submit-for-approval")
    def submit_for_approval(self, request, uuid=None):
        extraction_run = self.get_object()

        serializer = SubmitExtractionForApprovalSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        approval = ExtractionApprovalService.submit_for_approval(
            user=request.user,
            extraction_run=extraction_run,
            assigned_to=serializer.validated_data.get("assigned_to"),
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

        response_serializer = ApprovalRequestDetailSerializer(
            approval,
            context=self.get_serializer_context(),
        )

        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="fields")
    def fields(self, request, uuid=None):
        extraction_run = self.get_object()

        serializer = ExtractionResultFieldSerializer(
            extraction_run.result_fields.filter(is_deleted=False),
            many=True,
            context=self.get_serializer_context(),
        )

        return Response(serializer.data)

    @action(
        detail=True,
        methods=["patch"],
        url_path="fields/(?P<field_uuid>[^/.]+)/correct",
    )
    def correct_field(self, request, uuid=None, field_uuid=None):
        extraction_run = self.get_object()

        result_field = ExtractionResultField.objects.filter(
            uuid=field_uuid,
            extraction_run=extraction_run,
            is_deleted=False,
        ).first()

        if not result_field:
            return Response(
                {"detail": "Extraction field not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ExtractionFieldCorrectionSerializer(
            result_field,
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        result_field = serializer.save()

        response_serializer = ExtractionResultFieldSerializer(
            result_field,
            context=self.get_serializer_context(),
        )

        return Response(response_serializer.data)
