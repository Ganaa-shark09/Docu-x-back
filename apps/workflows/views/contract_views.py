from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count
from django.http import Http404, JsonResponse
from rest_framework import mixins, viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workflows.services.contract_milestone_service import ContractMilestoneService
from apps.workflows.services.contract_obligation_service import ContractObligationService
from apps.workflows.models import ContractReview, WorkflowRun, ContractObligationAssignment
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.serializers.contract_serializers import (
    ContractCorrectionSerializer,
    ContractObligationAssignSerializer,
    ContractObligationTransitionSerializer,
    ContractObligationSerializer,
    ContractRegisterSerializer,
    ContractDefinitionSerializer,
    ContractOutputSerializer,
    ContractReviewSerializer,
)
from apps.workflows.serializers.workflow_serializers import (
    WorkflowDefinitionSerializer,
    WorkflowVersionSerializer,
)
from apps.workflows.services.contract_definition_service import (
    ContractDefinitionService,
)
from apps.workflows.services.workflow_events import workflow_event


class ContractRegisterPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class ContractReviewViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    permission_classes = [IsAuthenticated]
    serializer_class = ContractReviewSerializer
    lookup_field = "uuid"
    pagination_class = ContractRegisterPagination

    def get_serializer_class(self):
        return (
            ContractRegisterSerializer
            if self.action == "list"
            else ContractReviewSerializer
        )

    def register_queryset(self):
        """Same source permissions as detail, enforced before counting/pagination."""
        user = self.request.user
        queryset = ContractReview.objects.filter(
            company_id=user.company_id,
            is_deleted=False,
            workflow_run__in=WorkflowPermissionService.visible_runs(
                user, WorkflowRun.objects.all()
            ),
        ).exclude(
            document_id__in=FieldPermissionService.denied_document_ids(user, "view")
        )
        status = self.request.query_params.get("status", "").strip()
        if status:
            if status not in WorkflowRun.Status.values:
                raise ValidationError({"status": "Unsupported workflow status."})
            queryset = queryset.filter(workflow_run__status=status)
        search = self.request.query_params.get("search", "").strip()[:100]
        if search:
            queryset = queryset.filter(document__title__icontains=search)
        return queryset.select_related("document", "workflow_run").order_by(
            "-created_at", "-pk"
        )

    def list(self, request, *args, **kwargs):
        queryset = self.register_queryset()
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = ContractRegisterSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        return Response(ContractRegisterSerializer(queryset, many=True).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Counts only records the caller could open, using the same filters as list."""
        rows = self.register_queryset().order_by().values(
            "workflow_run__status"
        ).annotate(count=Count("pk"))
        by_status = {key: 0 for key in WorkflowRun.Status.values}
        for row in rows:
            by_status[row["workflow_run__status"]] = row["count"]
        return Response({
            "total": sum(by_status.values()),
            "needs_attention": sum(by_status[key] for key in (
                "review_required", "approval_required", "exception", "failed"
            )),
            "by_status": by_status,
        })

    @action(detail=True, methods=["get"])
    def milestones(self, request, uuid=None):
        return Response(ContractMilestoneService.snapshot(self.get_object()))


    @action(detail=True, methods=["get", "post"], url_path="obligation-tracker")
    def obligation_tracker(self, request, uuid=None):
        review = self.get_object()
        if request.method == "POST":
            serializer = ContractObligationAssignSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            item = ContractObligationService.assign(
                request.user, review, **serializer.validated_data
            )
            return Response(ContractObligationSerializer(item).data, status=201)
        items = ContractObligationAssignment.objects.filter(
            review=review, company_id=request.user.company_id, is_deleted=False
        ).select_related("owner", "review", "review__document").prefetch_related(
            "events", "events__actor"
        ).order_by("source_revision", "source_index")
        return Response(ContractObligationSerializer(items, many=True).data)

    @action(
        detail=True, methods=["post"],
        url_path=r"obligation-tracker/(?P<item_uuid>[^/.]+)/transition",
    )
    def obligation_transition(self, request, uuid=None, item_uuid=None):
        review = self.get_object()
        serializer = ContractObligationTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = ContractObligationService.transition(
            request.user, review, item_uuid=item_uuid, **serializer.validated_data
        )
        return Response(ContractObligationSerializer(item).data)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def get_queryset(self):
        return (
            ContractReview.objects.filter(
                is_deleted=False,
                company_id=self.request.user.company_id,
                workflow_run__in=WorkflowPermissionService.visible_runs(
                    self.request.user, WorkflowRun.objects.all()
                ),
            )
            .select_related(
                "document",
                "document__text_extraction",
                "workflow_run",
                "extraction_run",
            )
            .prefetch_related(
                "outputs", "extraction_run__result_fields", "document__field_policies"
            )
        )

    def get_object(self):
        obj = super().get_object()
        if not WorkflowPermissionService.content_allowed(
            self.request.user, obj.workflow_run
        ):
            raise PermissionDenied(
                "Current contract field permissions deny content access."
            )
        return obj

    @action(detail=False, methods=["post"], url_path="definitions")
    @transaction.atomic
    def definitions(self, request):
        serializer = ContractDefinitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            definition, version = ContractDefinitionService.create(
                request.user, **serializer.validated_data
            )
        except DjangoValidationError:
            raise ValidationError(
                "Contract definition conflicts with ownership or an existing definition."
            )
        return Response(
            {
                "definition": WorkflowDefinitionSerializer(
                    definition, context=self.get_serializer_context()
                ).data,
                "version": WorkflowVersionSerializer(
                    version, context=self.get_serializer_context()
                ).data,
            },
            status=201,
        )

    @action(detail=True, methods=["get"])
    def fields(self, request, uuid=None):
        review = self.get_object()
        provenance = self.get_serializer(review).data["field_provenance"]
        fields = (
            review.extraction_run.result_fields.prefetch_related("corrections").all()
            if review.extraction_run_id
            else []
        )
        return Response(
            [
                {
                    **provenance[f.field_key],
                    "field_key": f.field_key,
                    "corrections": ContractCorrectionSerializer(
                        f.corrections.all(), many=True
                    ).data,
                }
                for f in fields
            ]
        )

    @action(detail=True, methods=["get"])
    def outputs(self, request, uuid=None):
        return Response(
            ContractOutputSerializer(self.get_object().outputs.all(), many=True).data
        )

    @action(detail=True, methods=["get"])
    def parties(self, request, uuid=None):
        return Response(self.get_object().normalized.get("parties") or [])

    @action(detail=True, methods=["get"])
    def obligations(self, request, uuid=None):
        return Response(self.get_object().normalized.get("obligations") or [])

    @action(detail=True, methods=["get"])
    def clauses(self, request, uuid=None):
        return Response(self.get_object().normalized.get("key_clauses") or [])

    @action(detail=True, methods=["get"], url_path="clause-analysis")
    def clause_analysis(self, request, uuid=None):
        from apps.workflows.services.clause_analysis_service import (
            ClauseAnalysisService,
        )

        analysis = self.get_object().clause_analyses.first()
        if not analysis:
            raise Http404
        ClauseAnalysisService.authorize(request.user, analysis)
        return Response(ClauseAnalysisService.snapshot(analysis))

    @action(detail=True, methods=["get"])
    def evidence(self, request, uuid=None):
        return Response(self.get_serializer(self.get_object()).data["field_provenance"])

    @action(detail=True, methods=["get"])
    def export(self, request, uuid=None):
        review = self.get_object()
        DocumentPermissionService.require_action(
            request.user, review.document, "export"
        )
        if FieldPermissionService.has_restrictions(
            request.user, review.document, "export"
        ):
            raise PermissionDenied("Current contract field policies deny export.")
        if review.workflow_run.status != "completed":
            raise ValidationError("Contract export requires a completed workflow.")
        output = next(iter(review.outputs.all()), None)
        if not output:
            raise Http404
        workflow_event(
            review.workflow_run,
            request.user,
            "contract_export_prepared",
            contract_revision=output.revision,
            digest=output.digest,
        )
        response = JsonResponse(output.data)
        response["Content-Disposition"] = (
            f'attachment; filename="contract-{review.uuid}-r{output.revision}.json"'
        )
        response["Cache-Control"] = "no-store"
        return response
