from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from django.db.models import Prefetch
from django.db.models import Q
from apps.workflows.models.compliance import ComplianceRequirement
from apps.workflows.services import compliance_schema

from apps.workflows.serializers import compliance_serializers as commands
from apps.workflows.serializers.workflow_serializers import WorkflowRunSerializer
from apps.workflows.services.compliance_library_service import (
    ComplianceLibraryService as library,
)
from apps.workflows.services.compliance_assessment_service import (
    ComplianceAssessmentService as service,
)
from apps.workflows.services.compliance_evidence_service import (
    ComplianceEvidenceService,
)
from apps.workflows.services.compliance_workflow_service import (
    ComplianceWorkflowService,
)
from apps.workflows.views.clause_views import NoStoreViewSet, command


def framework_data(row):
    return {
        "uuid": str(row.uuid),
        "code": row.code,
        "name": row.name,
        "description": row.description,
        "source": row.source,
        "jurisdiction": row.jurisdiction,
        "scope": "system" if row.company_id is None else "company",
        "revision": row.revision,
        "is_active": row.is_active,
        "active_version_uuid": str(row.active_version.uuid)
        if row.active_version_id
        else None,
        "draft": row.draft,
    }


def control_data(row):
    return {
        "uuid": str(row.uuid),
        "code": row.code,
        "name": row.name,
        "department": row.department_id,
        "definition": row.definition,
        "revision": row.revision,
        "is_active": row.is_active,
    }


def assessment_data(row):
    return {
        "uuid": str(row.uuid),
        "title": row.title,
        "department": row.department_id,
        "revision": row.revision,
        "framework_version_uuid": str(row.framework_version.uuid),
        "status": row.run.status if row.run_id else "draft",
        "run_uuid": str(row.run.uuid) if row.run_id else None,
    }


class ComplianceFrameworkViewSet(NoStoreViewSet):
    serializer_class = commands.FrameworkCreateSerializer

    def get_queryset(self):
        rows = library.frameworks(self.request.user)
        search = self.request.query_params.get("search", "").strip()[:200]
        if search:
            rows = rows.filter(Q(name__icontains=search) | Q(code__icontains=search))
        return rows.order_by("code", "pk")

    def list(self, request):
        return self.get_paginated_response(
            [framework_data(r) for r in self.paginate_queryset(self.get_queryset())]
        )

    def retrieve(self, request, uuid=None):
        return Response(framework_data(self.get_object()))

    def create(self, request):
        return Response(
            framework_data(
                library.create_framework(
                    request.user, **command(commands.FrameworkCreateSerializer, request)
                )
            ),
            status=201,
        )

    @action(detail=True, methods=["post"])
    def draft(self, request, uuid=None):
        return Response(
            framework_data(
                library.draft(
                    request.user,
                    self.get_object(),
                    **command(commands.DraftSerializer, request),
                )
            )
        )

    @action(detail=True, methods=["post"])
    def publish(self, request, uuid=None):
        result = library.publish(
            request.user,
            self.get_object(),
            **command(commands.RevisionSerializer, request),
        )
        return Response(
            {
                "uuid": str(result.uuid),
                "number": result.number,
                "digest": result.digest,
            },
            status=201,
        )

    @action(detail=True, methods=["post"])
    def activate(self, request, uuid=None):
        return Response(
            framework_data(
                library.activate(
                    request.user,
                    self.get_object(),
                    **command(commands.ActivateSerializer, request),
                )
            )
        )

    @action(detail=True, methods=["get"])
    def versions(self, request, uuid=None):
        framework = self.get_object()
        page = self.paginate_queryset(
            framework.versions.order_by("-number").prefetch_related(
                Prefetch(
                    "requirements",
                    queryset=ComplianceRequirement.objects.select_related(
                        "parent"
                    ).order_by("code"),
                )
            )
        )
        return self.get_paginated_response(
            [
                {
                    "uuid": str(v.uuid),
                    "number": v.number,
                    "metadata": v.metadata,
                    "digest": v.digest,
                    "state": "retired"
                    if not framework.is_active
                    else "active"
                    if framework.active_version_id == v.pk
                    else "superseded",
                    "requirements": [
                        {
                            "uuid": str(r.uuid),
                            "code": r.code,
                            "title": r.title,
                            "parent": str(r.parent.uuid) if r.parent_id else None,
                            "category": r.category,
                            "type": r.requirement_type,
                            "obligation": r.obligation,
                            "applicability": r.applicability,
                            "source": r.source,
                        }
                        for r in v.requirements.all()
                    ],
                }
                for v in page
            ]
        )

    @action(detail=True, methods=["get"])
    def requirements(self, request, uuid=None):
        framework = self.get_object()
        version_uuid = request.query_params.get("version_uuid")
        version = (
            framework.versions.filter(uuid=compliance_schema.uuid(version_uuid)).first()
            if version_uuid
            else framework.active_version
        )
        if not version:
            raise ValidationError(
                "Select a published version belonging to this framework."
            )
        page = self.paginate_queryset(
            version.requirements.select_related("parent").order_by("code")
        )
        return self.get_paginated_response(
            [
                {
                    "uuid": str(r.uuid),
                    "code": r.code,
                    "title": r.title,
                    "category": r.category,
                    "type": r.requirement_type,
                    "obligation": r.obligation,
                    "parent": str(r.parent.uuid) if r.parent_id else None,
                    "applicability": r.applicability,
                    "source": r.source,
                    "framework_version_uuid": str(version.uuid),
                }
                for r in page
            ]
        )


class ComplianceControlViewSet(NoStoreViewSet):
    serializer_class = commands.ControlCreateSerializer

    def get_queryset(self):
        rows = library.controls(self.request.user)
        search = self.request.query_params.get("search", "").strip()[:200]
        if search:
            rows = rows.filter(Q(name__icontains=search) | Q(code__icontains=search))
        version = self.request.query_params.get("framework_version_uuid")
        if version:
            rows = rows.filter(
                mappings__requirement__version__uuid=compliance_schema.uuid(version),
                mappings__is_active=True,
                mappings__is_deleted=False,
                mappings__company_id=self.request.user.company_id,
            ).distinct()
        return rows.order_by("code", "pk")

    def list(self, request):
        return self.get_paginated_response(
            [control_data(r) for r in self.paginate_queryset(self.get_queryset())]
        )

    def retrieve(self, request, uuid=None):
        return Response(control_data(self.get_object()))

    def create(self, request):
        return Response(
            control_data(
                library.create_control(
                    request.user, **command(commands.ControlCreateSerializer, request)
                )
            ),
            status=201,
        )

    @action(detail=True, methods=["post"])
    def configure(self, request, uuid=None):
        return Response(
            control_data(
                library.update_control(
                    request.user,
                    self.get_object(),
                    **command(commands.ControlUpdateSerializer, request),
                )
            )
        )

    @action(detail=True, methods=["post"])
    def mapping(self, request, uuid=None):
        row = library.map_control(
            request.user,
            self.get_object(),
            **command(commands.MappingSerializer, request),
        )
        row.control.refresh_from_db()
        return Response(
            {
                "uuid": str(row.uuid),
                "requirement_uuid": str(row.requirement.uuid),
                "control_uuid": str(row.control.uuid),
                "is_active": row.is_active,
                "revision": row.control.revision,
            }
        )

    @action(detail=True, methods=["get"])
    def mappings(self, request, uuid=None):
        page = self.paginate_queryset(
            self.get_object()
            .mappings.filter(company=request.user.company, is_deleted=False)
            .select_related("requirement__version")
            .order_by("pk")
        )
        return self.get_paginated_response(
            [
                {
                    "uuid": str(m.uuid),
                    "requirement_uuid": str(m.requirement.uuid),
                    "framework_version_uuid": str(m.requirement.version.uuid),
                    "is_active": m.is_active,
                }
                for m in page
            ]
        )


class ComplianceAssessmentViewSet(NoStoreViewSet):
    serializer_class = commands.AssessmentCreateSerializer

    def get_queryset(self):
        return service.visible(self.request.user).order_by("-pk")

    def assessment(self):
        return service.authorize(self.request.user, self.get_object())

    @action(detail=True, methods=["get"], url_path="source-options")
    def source_options(self, request, uuid=None):
        """Bounded source choices, using the same resolver as evidence linking.

        Each candidate must pass current source, field and workflow permissions.
        No output data or source snippets are returned by this directory.
        """
        from django.apps import apps

        assessment = self.assessment()
        kind = compliance_schema.choice(
            request.query_params.get("kind", "document"), compliance_schema.SOURCE_KINDS
        )
        registry = {
            "document": ("documents.Document", "company_id", "title"),
            "hr_policy_version": (
                "workflows.HRPolicyVersion",
                "policy__company_id",
                "title",
            ),
            "contract_output": (
                "workflows.ContractReviewOutput",
                "contract_review__company_id",
                "contract_review__document__title",
            ),
            "clause_output": (
                "workflows.ClauseAnalysisOutput",
                "analysis__company_id",
                "analysis__document__title",
            ),
            "finance_output": (
                "workflows.FinanceAuditOutput",
                "audit__company_id",
                "audit__title",
            ),
            "rfp_output": (
                "workflows.RFPProposalOutput",
                "project__company_id",
                "project__title",
            ),
            "invoice_output": (
                "workflows.InvoiceOutputVersion",
                "invoice__company_id",
                "invoice__document__title",
            ),
            "workflow_artifact": (
                "workflows.WorkflowArtifact",
                "run__company_id",
                "run__version__definition__name",
            ),
        }
        model, company_field, title_field = registry[kind]
        rows = apps.get_model(model).objects.filter(
            **{company_field: request.user.company_id}, is_deleted=False
        )
        if kind == "document":
            rows = rows.filter(uuid__in=assessment.scope["document_uuids"])
        query = request.query_params.get("search", "").strip()[:200]
        if query:
            rows = rows.filter(**{title_field + "__icontains": query})
        identifier = request.query_params.get("source_uuid")
        if identifier:
            rows = rows.filter(uuid=compliance_schema.uuid(identifier))
        try:
            page = int(request.query_params.get("page", "1"))
            if not 1 <= page <= 10000:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError({"page": "Choose a valid page."})
        # Candidate pagination avoids unbounded file/provenance validation.
        # A page can be empty if every candidate was revoked or is out of scope.
        candidates = list(rows.order_by("pk")[(page - 1) * 25 : page * 25 + 1])
        result = []
        for row in candidates[:25]:
            try:
                _, current = ComplianceEvidenceService.resolve(
                    request.user, assessment, kind, row.uuid
                )
            except (PermissionDenied, ValidationError):
                continue
            title = row
            for part in title_field.split("__"):
                title = getattr(title, part)
            version = getattr(row, "number", getattr(row, "revision", None))
            result.append(
                {
                    "uuid": str(row.uuid),
                    "title": str(title),
                    "status": "Current" if current else "Historical",
                    "code": "Version " + str(version)
                    if version is not None
                    else kind.replace("_", " "),
                }
            )
        params = request.query_params.copy()
        params["page"] = page + 1
        return Response(
            {
                "results": result,
                "next": request.path + "?" + params.urlencode()
                if len(candidates) > 25
                else None,
                "previous": page > 1,
            }
        )

    def list(self, request):
        return self.get_paginated_response(
            [
                assessment_data(service.authorize(request.user, r))
                for r in self.paginate_queryset(self.get_queryset())
            ]
        )

    def create(self, request):
        return Response(
            assessment_data(
                service.create(
                    request.user,
                    **command(commands.AssessmentCreateSerializer, request),
                )
            ),
            status=201,
        )

    def retrieve(self, request, uuid=None):
        assessment = self.assessment()
        return Response(
            {**assessment_data(assessment), **service.summary(request.user, assessment)}
        )

    @action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        run = ComplianceWorkflowService.submit(
            request.user,
            self.assessment(),
            **command(commands.SubmitSerializer, request),
        )
        return Response(WorkflowRunSerializer(run, context={"request": request}).data)

    @action(detail=True, methods=["get", "post"])
    def evidence(self, request, uuid=None):
        assessment = self.assessment()
        if request.method == "POST":
            row = service.link(
                request.user,
                assessment,
                **command(commands.EvidenceSerializer, request),
            )
            assessment.refresh_from_db()
            return Response(
                {"uuid": str(row.uuid), "revision": assessment.revision}, status=201
            )
        states = service.evidence_states(request.user, assessment)
        page = self.paginate_queryset(assessment.evidence.order_by("pk"))
        return self.get_paginated_response([states[str(r.uuid)] for r in page])

    @action(detail=True, methods=["post"])
    def discover(self, request, uuid=None):
        return Response(
            ComplianceEvidenceService.discover(
                request.user,
                self.assessment(),
                **command(commands.DiscoverySerializer, request),
            )
        )

    @action(detail=True, methods=["post"])
    def evaluate(self, request, uuid=None):
        assessment = self.assessment()
        row = service.evaluate(
            request.user, assessment, **command(commands.EvaluationSerializer, request)
        )
        assessment.refresh_from_db()
        return Response(
            {"uuid": str(row.uuid), "data": row.data, "revision": assessment.revision},
            status=201,
        )

    @action(detail=True, methods=["get", "post"])
    def decisions(self, request, uuid=None):
        assessment = self.assessment()
        if request.method == "POST":
            row = service.decide(
                request.user,
                assessment,
                **command(commands.DecisionSerializer, request),
            )
            assessment.refresh_from_db()
            return Response(
                {"uuid": str(row.uuid), "revision": assessment.revision}, status=201
            )
        page = self.paginate_queryset(
            assessment.decisions.select_related("evaluation").order_by("-pk")
        )
        return self.get_paginated_response(
            [
                {
                    "uuid": str(d.uuid),
                    "kind": d.kind,
                    "target": d.target,
                    "outcome": d.outcome,
                    "reason": d.reason,
                    "actor": d.actor_id,
                    "created_at": d.created_at,
                    "data": d.data,
                    "evaluation_uuid": str(d.evaluation.uuid)
                    if d.evaluation_id
                    else None,
                }
                for d in page
            ]
        )

    @action(detail=True, methods=["get"])
    def evaluations(self, request, uuid=None):
        page = self.paginate_queryset(self.assessment().evaluations.order_by("-pk"))
        return self.get_paginated_response(
            [
                {
                    "uuid": str(e.uuid),
                    "fingerprint": e.fingerprint,
                    "evidence_revision": e.evidence_revision,
                    "data": e.data,
                    "created_at": e.created_at,
                }
                for e in page
            ]
        )

    @action(detail=True, methods=["get"])
    def outputs(self, request, uuid=None):
        page = self.paginate_queryset(self.assessment().outputs.order_by("-pk"))
        return self.get_paginated_response(
            [
                {
                    "uuid": str(o.uuid),
                    "digest": o.digest,
                    "created_at": o.created_at,
                    "data": o.data,
                }
                for o in page
            ]
        )

    @action(detail=True, methods=["post"])
    def export(self, request, uuid=None):
        assessment = self.assessment()
        values = command(commands.ExportSerializer, request)
        output = (
            assessment.outputs.select_related("run")
            .filter(uuid=values["output_uuid"])
            .first()
        )
        if not output:
            raise ValidationError("Output unavailable.")
        return Response(
            service.export(request.user, assessment, output, values["external"])
        )
