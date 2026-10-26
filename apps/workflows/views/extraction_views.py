from django.db.models import Q
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from rest_framework.exceptions import PermissionDenied
from apps.audit.models import AuditLog
from apps.audit.services import AuditLogService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workflows.permissions.extraction_permissions import (
    ExtractionPermission,
    ExtractionTemplatePermission,
)

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
    permission_classes = [ExtractionTemplatePermission]
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

    @transaction.atomic
    def perform_destroy(self, instance):
        instance.soft_delete()
        self.log_template(instance, "delete")

    def log_template(self, instance, operation):
        AuditLogService.log(
            company=self.request.user.company,
            user=self.request.user,
            event_type=AuditLog.EventType.GOVERNANCE_POLICY_CHANGED,
            action="Extraction template changed.",
            resource_type="extraction_template",
            resource_uuid=instance.uuid,
            metadata={"operation": operation},
        )

    @transaction.atomic
    def perform_create(self, serializer):
        self.log_template(serializer.save(), "create")

    @transaction.atomic
    def perform_update(self, serializer):
        self.log_template(serializer.save(), "update")

    @action(detail=True, methods=["post"], url_path="fields")
    @transaction.atomic
    def add_field(self, request, uuid=None):
        template = self.get_object()

        serializer = ExtractionTemplateFieldSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(template=template)
        self.log_template(template, "field_created")

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ExtractionRunViewSet(viewsets.ModelViewSet):
    permission_classes = [ExtractionPermission]
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

        allowed_document_ids = (
            DocumentPermissionService.get_accessible_document_queryset(
                user=user,
                required_access="view",
            ).values_list("id", flat=True)
        )

        from apps.workflows.models import WorkflowRun
        from apps.workflows.permissions.workflow_permissions import (
            WorkflowPermissionService,
        )

        visible_runs = WorkflowPermissionService.visible_runs(
            user, WorkflowRun.objects.all()
        ).values("pk")
        queryset = queryset.filter(
            Q(workflow_step_run__isnull=True)
            | Q(workflow_step_run__run_id__in=visible_runs)
        )
        from apps.workflows.models import ContractReview

        protected_ids = FieldPermissionService.denied_document_ids(user, "view")
        queryset = queryset.exclude(
            pk__in=ContractReview.objects.filter(document_id__in=protected_ids).values(
                "extraction_run_id"
            )
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
    @transaction.atomic
    def rerun(self, request, uuid=None):
        extraction_run = self.get_object()
        from apps.workflows.models import ContractReview

        if ContractReview.objects.filter(extraction_run=extraction_run).exists():
            return Response(
                {
                    "detail": "Start a new workflow for a new contract extraction revision."
                },
                status=409,
            )
        extraction_run = ExtractionRun.objects.select_for_update().get(
            pk=extraction_run.pk
        )
        DocumentPermissionService.require_action(
            request.user, extraction_run.document, "extract"
        )
        if extraction_run.status in {
            ExtractionRun.Status.APPROVED,
            ExtractionRun.Status.SUBMITTED_FOR_APPROVAL,
            ExtractionRun.Status.RUNNING,
            ExtractionRun.Status.PENDING,
        }:
            return Response(
                {"detail": "This extraction cannot be rerun in its current state."},
                status=409,
            )

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

        transaction.on_commit(
            lambda: run_structured_extraction_task.delay(str(extraction_run.uuid))
        )

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

        if not FieldPermissionService.field_allowed(
            request.user, extraction_run.document, result_field.field_key
        ):
            raise PermissionDenied(
                "Restricted fields cannot be corrected without view permission."
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
