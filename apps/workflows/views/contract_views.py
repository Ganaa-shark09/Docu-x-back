from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import Http404, JsonResponse
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workflows.models import ContractReview, WorkflowRun
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.serializers.contract_serializers import (
    ContractCorrectionSerializer,
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


class ContractReviewViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ContractReviewSerializer
    lookup_field = "uuid"

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
