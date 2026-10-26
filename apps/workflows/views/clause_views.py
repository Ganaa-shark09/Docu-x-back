from django.db import transaction, IntegrityError
from django.db.models import Q
from django.http import JsonResponse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workflows.models import (
    ClauseType,
    ClauseAnalysis,
    ClauseCorrection,
    WorkflowRun,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.serializers.clause_serializers import (
    TaxonomyWriteSerializer,
    PlaybookCreateSerializer,
    PlaybookDraftSerializer,
    PlaybookActivationSerializer,
    RevisionSerializer,
    ClauseDecisionSerializer,
    ClauseCorrectionSerializer,
    ClauseReanalysisSerializer,
    ClauseDefinitionSerializer,
)
from apps.workflows.serializers.workflow_serializers import (
    WorkflowDefinitionSerializer,
    WorkflowVersionSerializer,
    WorkflowRunSerializer,
)
from apps.workflows.services.clause_analysis_service import ClauseAnalysisService
from apps.workflows.services.clause_playbook_service import ClausePlaybookService
from apps.workflows.services.clause_definition_service import ClauseDefinitionService
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_events import workflow_event


def command(serializer_class, request):
    serializer = serializer_class(data=request.data)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


def taxonomy_data(obj):
    return {
        key: getattr(obj, key)
        for key in (
            "uuid",
            "code",
            "name",
            "description",
            "category",
            "aliases",
            "document_types",
            "fact_schema",
            "is_active",
        )
    } | {"scope": "system" if obj.company_id is None else "company"}


def version_data(obj):
    return {
        key: getattr(obj, key)
        for key in (
            "uuid",
            "number",
            "rules",
            "taxonomy",
            "document_types",
            "effective_from",
            "effective_until",
            "created_at",
        )
    } | {"playbook_uuid": str(obj.playbook.uuid)}


def book_data(obj):
    return {
        key: getattr(obj, key)
        for key in (
            "uuid",
            "name",
            "description",
            "department_id",
            "draft",
            "revision",
            "is_active",
        )
    } | {
        "active_version_uuid": str(obj.active_version.uuid)
        if obj.active_version_id
        else None
    }


class ClausePagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class NoStoreViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    lookup_field = "uuid"
    pagination_class = ClausePagination

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


class ClauseTypeViewSet(NoStoreViewSet):
    serializer_class = TaxonomyWriteSerializer

    def get_queryset(self):
        if not WorkflowPermissionService.active(self.request.user):
            return ClauseType.objects.none()
        return ClauseType.objects.filter(
            Q(company_id=self.request.user.company_id) | Q(company__isnull=True),
            is_deleted=False,
        ).order_by("code", "uuid")

    def list(self, request):
        page = self.paginate_queryset(self.get_queryset())
        rows = (
            [taxonomy_data(obj) for obj in page]
            if page is not None
            else [taxonomy_data(obj) for obj in self.get_queryset()[:100]]
        )
        return self.get_paginated_response(rows) if page is not None else Response(rows)

    def retrieve(self, request, uuid=None):
        return Response(taxonomy_data(self.get_object()))

    def create(self, request):
        WorkflowDefinitionService.require_admin(request.user, None)
        attrs = command(TaxonomyWriteSerializer, request)
        try:
            with transaction.atomic():
                obj = ClauseType.objects.create(
                    company_id=request.user.company_id, **attrs
                )
                self.audit(obj, request.user, "clause_taxonomy_created")
        except IntegrityError:
            raise ValidationError(
                "A company clause type with this code already exists."
            )
        return Response(taxonomy_data(obj), status=201)

    @transaction.atomic
    def partial_update(self, request, uuid=None):
        WorkflowDefinitionService.require_admin(request.user, None)
        obj = self.get_queryset().select_for_update().get(pk=self.get_object().pk)
        if obj.company_id is None:
            raise PermissionDenied(
                "System clause taxonomy cannot be changed by tenants."
            )
        serializer = TaxonomyWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if (
            "code" in serializer.validated_data
            and serializer.validated_data["code"] != obj.code
        ):
            raise ValidationError(
                "Clause taxonomy codes are stable; create a new definition."
            )
        for key, value in serializer.validated_data.items():
            setattr(obj, key, value)
        obj.save()
        self.audit(obj, request.user, "clause_taxonomy_updated")
        return Response(taxonomy_data(obj))

    @staticmethod
    def audit(obj, user, event):
        from apps.audit.services import AuditLogService

        AuditLogService.log(
            company=user.company,
            user=user,
            event_type="workflow_event",
            action="Legal taxonomy configuration changed.",
            resource_type="clause_type",
            resource_uuid=obj.uuid,
            metadata={"event": event},
        )


class ClausePlaybookViewSet(NoStoreViewSet):
    serializer_class = PlaybookCreateSerializer

    def get_queryset(self):
        return ClausePlaybookService.visible(self.request.user).order_by("uuid")

    def get_object(self):
        obj = super().get_object()
        ClausePlaybookService.require(self.request.user, obj)
        return obj

    def list(self, request):
        page = self.paginate_queryset(self.get_queryset())
        rows = (
            [book_data(obj) for obj in page]
            if page is not None
            else [book_data(obj) for obj in self.get_queryset()[:100]]
        )
        return self.get_paginated_response(rows) if page is not None else Response(rows)

    def retrieve(self, request, uuid=None):
        return Response(book_data(self.get_object()))

    def create(self, request):
        obj = ClausePlaybookService.create(
            request.user, **command(PlaybookCreateSerializer, request)
        )
        return Response(book_data(obj), status=201)

    @action(detail=True, methods=["patch"], url_path="draft")
    def edit_draft(self, request, uuid=None):
        attrs = command(PlaybookDraftSerializer, request)
        expected = attrs.pop("expected_revision")
        return Response(
            book_data(
                ClausePlaybookService.edit(
                    request.user, self.get_object(), expected, **attrs
                )
            )
        )

    @action(detail=True, methods=["post"])
    def publish(self, request, uuid=None):
        attrs = command(RevisionSerializer, request)
        obj = ClausePlaybookService.publish(
            request.user, self.get_object(), attrs["expected_revision"]
        )
        return Response(version_data(obj), status=201)

    @action(detail=True, methods=["post"])
    def activate(self, request, uuid=None):
        attrs = command(PlaybookActivationSerializer, request)
        book = self.get_object()
        version = (
            book.versions.filter(uuid=attrs.get("version"), is_deleted=False).first()
            if attrs.get("version")
            else None
        )
        obj = ClausePlaybookService.activate(
            request.user,
            book,
            attrs["expected_revision"],
            version=version,
            active=attrs["active"],
        )
        return Response(book_data(obj))

    @action(detail=True, methods=["get"])
    def versions(self, request, uuid=None):
        page = self.paginate_queryset(
            self.get_object().versions.filter(is_deleted=False)
        )
        return self.get_paginated_response([version_data(v) for v in page])


class ClauseAnalysisViewSet(NoStoreViewSet):
    serializer_class = ClauseDecisionSerializer

    def get_queryset(self):
        return ClauseAnalysis.objects.filter(
            company_id=self.request.user.company_id,
            is_deleted=False,
            workflow_run__in=WorkflowPermissionService.visible_runs(
                self.request.user, WorkflowRun.objects.all()
            ),
        ).select_related(
            "workflow_run", "document", "playbook_version__playbook", "contract_review"
        )

    def get_object(self):
        obj = super().get_object()
        ClauseAnalysisService.authorize(
            self.request.user, obj, source=self.action != "reanalyze"
        )
        return obj

    def retrieve(self, request, uuid=None):
        return Response(ClauseAnalysisService.snapshot(self.get_object()))

    @action(detail=False, methods=["post"])
    def definitions(self, request):
        definition, version = ClauseDefinitionService.create(
            request.user, **command(ClauseDefinitionSerializer, request)
        )
        return Response(
            {
                "definition": WorkflowDefinitionSerializer(definition).data,
                "version": WorkflowVersionSerializer(version).data,
            },
            status=201,
        )

    @action(detail=True, methods=["get"])
    def clauses(self, request, uuid=None):
        analysis = self.get_object()
        return Response(ClauseAnalysisService.clauses(analysis))

    @action(detail=True, methods=["get"])
    def deviations(self, request, uuid=None):
        return Response(ClauseAnalysisService.snapshot(self.get_object())["deviations"])

    @action(detail=True, methods=["get"])
    def evidence(self, request, uuid=None):
        return Response(
            [
                {
                    "clause_uuid": row["uuid"],
                    "evidence": row["evidence"],
                    "fact_evidence": {
                        k: f["evidence"] for k, f in row["facts"].items()
                    },
                }
                for row in ClauseAnalysisService.clauses(self.get_object())
            ]
        )

    @action(detail=True, methods=["post"])
    def decision(self, request, uuid=None):
        obj = ClauseAnalysisService.decide(
            request.user,
            self.get_object(),
            **command(ClauseDecisionSerializer, request),
        )
        obj.analysis.workflow_run.refresh_from_db()
        return Response(
            {
                "uuid": str(obj.uuid),
                "revision": obj.revision,
                "workflow_revision": obj.analysis.workflow_run.revision,
                "decision": obj.decision,
            },
            status=201,
        )

    @action(detail=True, methods=["get"])
    def corrections(self, request, uuid=None):
        analysis = self.get_object()
        page = self.paginate_queryset(
            ClauseCorrection.objects.filter(clause__analysis=analysis).select_related(
                "clause__clause_type", "clause_type"
            )
        )
        return self.get_paginated_response(
            [
                {
                    "uuid": str(row.uuid),
                    "clause_uuid": str(row.clause.uuid),
                    "revision": row.revision,
                    "clause_type": str(row.clause_type.uuid),
                    "facts": row.facts,
                    "evidence": row.evidence,
                    "actor_id": row.actor_id,
                    "reason": row.reason,
                    "created_at": row.created_at,
                    "original": {
                        "clause_type": str(row.clause.clause_type.uuid),
                        "facts": row.clause.facts,
                        "evidence": row.clause.evidence,
                    },
                }
                for row in page
            ]
        )

    @action(detail=True, methods=["post"])
    def correct(self, request, uuid=None):
        obj = ClauseAnalysisService.correct(
            request.user,
            self.get_object(),
            **command(ClauseCorrectionSerializer, request),
        )
        return Response({"uuid": str(obj.uuid), "revision": obj.revision}, status=201)

    @action(detail=True, methods=["post"])
    def reanalyze(self, request, uuid=None):
        obj = ClauseAnalysisService.reanalyze(
            request.user,
            self.get_object(),
            **command(ClauseReanalysisSerializer, request),
        )
        return Response(
            WorkflowRunSerializer(obj, context={"request": request}).data, status=201
        )

    @action(detail=True, methods=["get"])
    def outputs(self, request, uuid=None):
        return Response(
            list(
                self.get_object().outputs.values(
                    "uuid", "revision", "data", "digest", "created_at"
                )
            )
        )

    @action(detail=True, methods=["get"])
    def export(self, request, uuid=None):
        analysis = self.get_object()
        DocumentPermissionService.require_action(
            request.user, analysis.document, "export"
        )
        if FieldPermissionService.has_restrictions(
            request.user, analysis.document, "export"
        ):
            raise PermissionDenied("Current clause field policies deny export.")
        output = analysis.outputs.filter(revision=analysis.revision).first()
        if analysis.workflow_run.status != "completed" or not output:
            raise ValidationError("Legal export requires a completed workflow output.")
        workflow_event(
            analysis.workflow_run,
            request.user,
            "clause_output_exported",
            analysis_uuid=str(analysis.uuid),
            revision=analysis.revision,
            digest=output.digest,
        )
        response = JsonResponse(output.data)
        response["Content-Disposition"] = (
            f'attachment; filename="clauses-{analysis.uuid}-r{output.revision}.json"'
        )
        response["Cache-Control"] = "no-store"
        return response
