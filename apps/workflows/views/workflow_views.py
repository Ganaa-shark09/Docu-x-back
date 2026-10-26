from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.workflows.models import WorkflowRun
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.serializers.workflow_serializers import (
    DefinitionStateSerializer,
    DefinitionWriteSerializer,
    FindingReviewSerializer,
    StartWorkflowSerializer,
    VersionPublishSerializer,
    WorkflowArtifactSerializer,
    WorkflowCommandSerializer,
    WorkflowCommentSerializer,
    WorkflowDefinitionSerializer,
    WorkflowExceptionSerializer,
    WorkflowFindingSerializer,
    WorkflowRunSerializer,
    WorkflowStepRunSerializer,
    WorkflowVersionSerializer,
)
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import WorkflowEngineService
from apps.workflows.services.workflow_events import workflow_event


class WorkflowDefinitionViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowDefinitionSerializer
    lookup_field = "uuid"

    def get_queryset(self):
        queryset = WorkflowPermissionService.definitions(self.request.user)
        workflow_type = self.request.query_params.get("type")
        if workflow_type:
            queryset = queryset.filter(workflow_type=workflow_type)
        return queryset.prefetch_related("versions")

    def create(self, request):
        serializer = DefinitionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attrs = dict(serializer.validated_data)
        attrs["department_id"] = attrs.pop("department", None)
        try:
            definition = WorkflowDefinitionService.create(request.user, **attrs)
        except DjangoValidationError as exc:
            raise ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            )
        return Response(
            self.get_serializer(definition).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def activation(self, request, uuid=None):
        serializer = DefinitionStateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            obj = (
                self.get_queryset()
                .select_for_update(of=("self",))
                .get(pk=self.get_object().pk)
            )
            WorkflowDefinitionService.require_admin(request.user, obj.department_id)
            obj.is_active = serializer.validated_data["is_active"]
            obj.save(update_fields=["is_active", "updated_at"])
            workflow_event(
                None,
                request.user,
                "definition_activation_changed",
                definition=obj,
                active=obj.is_active,
            )
        return Response(self.get_serializer(obj).data)

    @action(detail=True, methods=["get", "post"])
    def versions(self, request, uuid=None):
        definition = self.get_object()
        if request.method == "GET":
            return Response(
                WorkflowVersionSerializer(
                    definition.versions.all(),
                    many=True,
                    context=self.get_serializer_context(),
                ).data
            )
        serializer = VersionPublishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        version = WorkflowDefinitionService.publish(
            request.user, definition, **serializer.validated_data
        )
        return Response(
            WorkflowVersionSerializer(
                version, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )


class WorkflowRunViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowRunSerializer
    lookup_field = "uuid"

    def get_queryset(self):
        queryset = WorkflowPermissionService.visible_runs(
            self.request.user, WorkflowRun.objects.all()
        )
        state = self.request.query_params.get("status")
        if state:
            queryset = queryset.filter(status=state)
        return queryset.select_related(
            "version__definition", "current_step"
        ).prefetch_related("documents")

    def get_object(self):
        obj = super().get_object()
        WorkflowPermissionService.require(self.request.user, obj, "view")
        return obj

    def create(self, request):
        serializer = StartWorkflowSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attrs = dict(serializer.validated_data)
        attrs["version_uuid"], attrs["document_uuids"] = (
            attrs.pop("version"),
            attrs.pop("documents"),
        )
        run, created = WorkflowEngineService.start(request.user, **attrs)
        return Response(
            self.get_serializer(run).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def commands(self, request, uuid=None):
        serializer = WorkflowCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = WorkflowEngineService.command(
            request.user, self.get_object(), **serializer.validated_data
        )
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["get"])
    def steps(self, request, uuid=None):
        return Response(
            WorkflowStepRunSerializer(
                self.get_object().step_runs.select_related(
                    "step", "approval", "assignment"
                ),
                many=True,
            ).data
        )

    @action(detail=True, methods=["get"], url_path="clause-analysis")
    def clause_analysis(self, request, uuid=None):
        from django.http import Http404
        from apps.workflows.models import ClauseAnalysis
        from apps.workflows.services.clause_analysis_service import (
            ClauseAnalysisService,
        )

        analysis = ClauseAnalysis.objects.filter(
            workflow_run=self.get_object(), is_deleted=False
        ).first()
        if not analysis:
            raise Http404
        ClauseAnalysisService.authorize(request.user, analysis)
        response = Response(ClauseAnalysisService.snapshot(analysis))
        response["Cache-Control"] = "no-store"
        return response

    def content(self, serializer, relation):
        run = self.get_object()
        if not WorkflowPermissionService.content_allowed(self.request.user, run):
            raise PermissionDenied("Current field permissions deny workflow content.")
        return Response(
            serializer(getattr(run, relation).filter(is_deleted=False), many=True).data
        )

    @action(detail=True, methods=["get"])
    def findings(self, request, uuid=None):
        return self.content(WorkflowFindingSerializer, "findings")

    @action(
        detail=True,
        methods=["post"],
        url_path="findings/(?P<finding_uuid>[0-9a-f-]+)/review",
    )
    def review_finding(self, request, uuid=None, finding_uuid=None):
        serializer = FindingReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        data["owner_id"] = data.pop("owner", None)
        finding = WorkflowEngineService.review_finding(
            request.user, self.get_object(), finding_uuid, **data
        )
        return Response(WorkflowFindingSerializer(finding).data)

    @action(detail=True, methods=["get"])
    def exceptions(self, request, uuid=None):
        return self.content(WorkflowExceptionSerializer, "exceptions")

    @action(detail=True, methods=["get"])
    def comments(self, request, uuid=None):
        return self.content(WorkflowCommentSerializer, "comments")

    @action(detail=True, methods=["get"])
    def artifacts(self, request, uuid=None):
        return self.content(WorkflowArtifactSerializer, "artifacts")

    @action(detail=True, methods=["get"])
    def invoice(self, request, uuid=None):
        from django.http import Http404
        from apps.workflows.models import InvoiceRecord
        from apps.workflows.serializers.invoice_serializers import InvoiceSerializer

        run = self.get_object()
        if not WorkflowPermissionService.content_allowed(request.user, run):
            raise PermissionDenied("Current field permissions deny invoice content.")
        invoice = InvoiceRecord.objects.filter(
            workflow_run=run, is_deleted=False
        ).first()
        if not invoice:
            raise Http404
        return Response(
            InvoiceSerializer(invoice, context=self.get_serializer_context()).data
        )

    @action(detail=True, methods=["get"], url_path="contract-review")
    def contract_review(self, request, uuid=None):
        from django.http import Http404
        from apps.workflows.models import ContractReview
        from apps.workflows.serializers.contract_serializers import (
            ContractReviewSerializer,
        )

        run = self.get_object()
        if not WorkflowPermissionService.content_allowed(request.user, run):
            raise PermissionDenied("Current field permissions deny contract content.")
        review = (
            ContractReview.objects.filter(workflow_run=run, is_deleted=False)
            .select_related("document", "workflow_run", "extraction_run")
            .prefetch_related("outputs", "extraction_run__result_fields")
            .first()
        )
        if not review:
            raise Http404
        return Response(
            ContractReviewSerializer(review, context=self.get_serializer_context()).data
        )


class WorkflowVersionViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowVersionSerializer
    lookup_field = "uuid"

    def get_queryset(self):
        from apps.workflows.models import WorkflowVersion

        return WorkflowVersion.objects.filter(
            definition__in=WorkflowPermissionService.definitions(self.request.user),
            is_deleted=False,
        )
