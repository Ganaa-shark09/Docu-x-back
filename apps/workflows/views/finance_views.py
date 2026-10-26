from rest_framework.decorators import action
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from apps.workflows.models import WorkflowFinding
from apps.workflows.models.finance import FinanceCorrection
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services import finance_schema
from rest_framework.exceptions import PermissionDenied

from apps.workflows.serializers import finance_serializers as commands
from apps.workflows.serializers.workflow_serializers import WorkflowRunSerializer
from apps.workflows.services.finance_audit_service import FinanceAuditService as service
from apps.workflows.services.finance_workflow_service import FinanceWorkflowService
from apps.workflows.views.clause_views import NoStoreViewSet, command
from django.db.models import OuterRef, Subquery, JSONField, F, DecimalField
from django.db.models.functions import Coalesce, Cast
from django.db.models.fields.json import KeyTextTransform


def audit_data(audit):
    status = audit.run.status if audit.run_id else "draft"
    if (
        audit.run_id
        and status in {"completed", "approved", "rejected", "failed", "cancelled"}
        and audit.run.metadata.get("audit_revision") != audit.revision
    ):
        status = "draft"
    return {
        "uuid": str(audit.uuid),
        "title": audit.title,
        "department": audit.department_id,
        "revision": audit.revision,
        "population_revision": audit.population_revision,
        "status": status,
        "run_uuid": str(audit.run.uuid) if audit.run_id else None,
    }


def version_data(obj):
    return {
        "uuid": str(obj.uuid),
        "number": getattr(obj, "number", None),
        "created_at": obj.created_at,
    }


class FinanceAuditViewSet(NoStoreViewSet):
    serializer_class = commands.FinanceCreateSerializer

    @action(detail=False, methods=["get"], url_path="test-library")
    def test_library(self, request):
        if not WorkflowPermissionService.active(request.user):
            raise PermissionDenied("An active company is required.")
        return Response(
            {
                "version": 1,
                "protected": True,
                "tests": [
                    {"code": code, "name": code.replace("_", " ").title()}
                    for code in finance_schema.TESTS
                ],
                "sampling_methods": sorted(finance_schema.METHODS),
            }
        )

    def get_queryset(self):
        return service.visible(self.request.user).order_by("-pk")

    def audit(self):
        return service.authorize(self.request.user, self.get_object())

    def paginated(self, query, serializer):
        page = self.paginate_queryset(query)
        return self.get_paginated_response([serializer(row) for row in page])

    def list(self, request):
        # Revalidate every derivative returned on this bounded page.
        return self.paginated(
            self.get_queryset(),
            lambda row: audit_data(service.authorize(request.user, row)),
        )

    def create(self, request):
        audit = service.create(
            request.user, **command(commands.FinanceCreateSerializer, request)
        )
        return Response(audit_data(audit), status=201)

    def retrieve(self, request, uuid=None):
        audit = self.audit()
        return Response(
            {**audit_data(audit), "scope": audit.scope, "policy": audit.policy}
        )

    @action(detail=True, methods=["post"])
    def configure(self, request, uuid=None):
        audit = service.configure(
            request.user,
            self.audit(),
            **command(commands.FinanceConfigureSerializer, request),
        )
        return Response(audit_data(audit))

    @action(detail=True, methods=["get", "post"])
    def datasets(self, request, uuid=None):
        audit = self.audit()

        def data(row):
            return {
                **version_data(row),
                "document_uuid": str(row.document.uuid),
                "checksum": row.checksum,
                "mapping": row.mapping,
                "kind": row.kind,
                "row_count": row.row_count,
                "totals": row.totals,
            }

        if request.method == "POST":
            obj = service.ingest(
                request.user,
                audit,
                **command(commands.FinanceImportSerializer, request),
            )
            return Response(data(obj), status=201)
        return self.paginated(
            audit.datasets.select_related("document").order_by("number"), data
        )

    @action(detail=True, methods=["get"])
    def transactions(self, request, uuid=None):
        audit = self.audit()
        query = service.rows(audit)
        filters = {
            "currency": "currency",
            "transaction_type": "transaction_type",
            "vendor": "vendor_identifier",
            "identifier": "transaction_id",
            "account": "account_code",
        }
        if set(request.query_params) & (
            set(filters) | {"date_from", "date_to", "amount_min", "amount_max"}
        ):
            correction = (
                FinanceCorrection.objects.filter(transaction_id=OuterRef("pk"))
                .order_by("-number")
                .values("corrected")[:1]
            )
            query = query.annotate(
                _effective=Coalesce(
                    Subquery(correction, output_field=JSONField()), F("data")
                )
            )
        for parameter, field in filters.items():
            if request.query_params.get(parameter):
                query = query.filter(
                    **{"_effective__" + field: request.query_params[parameter][:150]}
                )
        for parameter, operation in (("date_from", "gte"), ("date_to", "lte")):
            if request.query_params.get(parameter):
                query = query.filter(
                    **{
                        "_effective__transaction_date__"
                        + operation: finance_schema.iso_date(
                            request.query_params[parameter]
                        ).isoformat()
                    }
                )
        if request.query_params.get("amount_min") or request.query_params.get(
            "amount_max"
        ):
            query = query.annotate(
                _amount=Cast(
                    KeyTextTransform("gross_amount", "_effective"),
                    DecimalField(max_digits=30, decimal_places=8),
                )
            )
            for parameter, operation in (("amount_min", "gte"), ("amount_max", "lte")):
                if request.query_params.get(parameter):
                    query = query.filter(
                        **{
                            "_amount__" + operation: finance_schema.money(
                                request.query_params[parameter], False
                            )
                        }
                    )

        def data(row):
            value, correction = service.effective(row)
            return {
                "uuid": str(row.uuid),
                "dataset_uuid": str(row.dataset.uuid),
                "row": row.row,
                "data": value,
                "provenance": row.provenance,
                "correction_uuid": str(correction.uuid) if correction else None,
            }

        return self.paginated(query, data)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"transactions/(?P<item>[0-9a-f-]{36})/correct",
    )
    def correct(self, request, uuid=None, item=None):
        audit = self.audit()
        get_object_or_404(service.rows(audit), uuid=item)
        obj = service.correct(
            request.user,
            audit,
            item,
            **command(commands.FinanceCorrectionSerializer, request),
        )
        return Response(version_data(obj), status=201)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"transactions/(?P<item>[0-9a-f-]{36})/support",
    )
    def support(self, request, uuid=None, item=None):
        audit = self.audit()
        get_object_or_404(service.rows(audit), uuid=item)
        obj = service.support(
            request.user,
            audit,
            item,
            **command(commands.FinanceSupportSerializer, request),
        )
        return Response(version_data(obj), status=201)

    @action(detail=True, methods=["get", "post"])
    def samples(self, request, uuid=None):
        audit = self.audit()

        def data(row):
            return {
                **version_data(row),
                "population_revision": row.population_revision,
                "population_digest": row.population_digest,
                "method": row.method,
                "parameters": row.parameters,
                "selected": row.selected,
            }

        if request.method == "POST":
            obj = service.sample(
                request.user,
                audit,
                **command(commands.FinanceSampleSerializer, request),
            )
            return Response(data(obj), status=201)
        return self.paginated(audit.samples.order_by("pk"), data)

    @action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        run = FinanceWorkflowService.submit(
            request.user,
            self.audit(),
            **command(commands.FinanceSubmitSerializer, request),
        )
        return Response(WorkflowRunSerializer(run).data, status=201)

    @action(detail=True, methods=["get", "post"], url_path="test-runs")
    def test_runs(self, request, uuid=None):
        audit = self.audit()

        def data(row):
            return {
                **version_data(row),
                "counts": row.counts,
                "definitions": row.definitions,
                "population_digest": row.population_digest,
                "population_revision": row.population_revision,
                "previous_uuid": str(row.previous.uuid) if row.previous_id else None,
                "sample_uuid": str(row.sample.uuid) if row.sample_id else None,
            }

        if request.method == "POST":
            obj = service.execute(
                request.user, audit, **command(commands.FinanceTestSerializer, request)
            )
            return Response(data(obj), status=201)
        return self.paginated(
            audit.test_runs.select_related("previous", "sample").order_by("pk"), data
        )

    @action(
        detail=True,
        methods=["get"],
        url_path=r"test-runs/(?P<test>[0-9a-f-]{36})/results",
    )
    def results(self, request, uuid=None, test=None):
        audit = self.audit()
        obj = get_object_or_404(audit.test_runs, uuid=test)
        query = obj.results.select_related("transaction", "finding").order_by("pk")
        for field in ("code", "status"):
            if request.query_params.get(field):
                query = query.filter(**{field: request.query_params[field][:50]})
        return self.paginated(
            query,
            lambda row: {
                "uuid": str(row.uuid),
                "transaction_uuid": str(row.transaction.uuid),
                "code": row.code,
                "status": row.status,
                "evidence": row.evidence,
                "finding_uuid": str(row.finding.uuid) if row.finding_id else None,
            },
        )

    @action(
        detail=True,
        methods=["post"],
        url_path=r"findings/(?P<finding>[0-9a-f-]{36})/respond",
    )
    def respond(self, request, uuid=None, finding=None):
        audit = self.audit()
        get_object_or_404(
            WorkflowFinding.objects.filter(run_id=audit.run_id), uuid=finding
        )
        obj = service.respond(
            request.user,
            audit,
            finding,
            **command(commands.FinanceResponseSerializer, request),
        )
        return Response(version_data(obj), status=201)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"findings/(?P<finding>[0-9a-f-]{36})/decide",
    )
    def decide(self, request, uuid=None, finding=None):
        audit = self.audit()
        get_object_or_404(
            WorkflowFinding.objects.filter(run_id=audit.run_id), uuid=finding
        )
        obj = service.decide(
            request.user,
            audit,
            finding,
            **command(commands.FinanceDecisionSerializer, request),
        )
        return Response(version_data(obj), status=201)

    @action(detail=True, methods=["get"])
    def outputs(self, request, uuid=None):
        audit = self.audit()
        return self.paginated(audit.outputs.order_by("number"), version_data)

    @action(detail=True, methods=["get"])
    def findings(self, request, uuid=None):
        audit = self.audit()
        query = WorkflowFinding.objects.filter(run_id=audit.run_id).order_by("pk")
        for field in ("status", "severity", "category"):
            if request.query_params.get(field):
                query = query.filter(**{field: request.query_params[field][:80]})
        return self.paginated(
            query,
            lambda row: {
                "uuid": str(row.uuid),
                "category": row.category,
                "severity": row.severity,
                "status": row.status,
                "review_state": row.review_state,
                "data": row.data,
                "evidence": row.evidence,
            },
        )

    @action(
        detail=True,
        methods=["get"],
        url_path=r"findings/(?P<finding>[0-9a-f-]{36})/responses",
    )
    def responses(self, request, uuid=None, finding=None):
        audit = self.audit()
        obj = get_object_or_404(
            WorkflowFinding.objects.filter(run_id=audit.run_id), uuid=finding
        )
        return self.paginated(
            audit.responses.filter(finding=obj).order_by("number"),
            lambda row: {**version_data(row), "actor": row.actor_id, "data": row.data},
        )

    @action(
        detail=True,
        methods=["get"],
        url_path=r"findings/(?P<finding>[0-9a-f-]{36})/decisions",
    )
    def decisions(self, request, uuid=None, finding=None):
        audit = self.audit()
        obj = get_object_or_404(
            WorkflowFinding.objects.filter(run_id=audit.run_id), uuid=finding
        )
        return self.paginated(
            audit.decisions.filter(finding=obj).order_by("number"),
            lambda row: {
                **version_data(row),
                "actor": row.actor_id,
                "decision": row.decision,
                "reason": row.reason,
            },
        )

    @action(
        detail=True,
        methods=["post"],
        url_path=r"findings/(?P<finding>[0-9a-f-]{36})/request-evidence",
    )
    def request_evidence(self, request, uuid=None, finding=None):
        audit = self.audit()
        get_object_or_404(
            WorkflowFinding.objects.filter(run_id=audit.run_id), uuid=finding
        )
        obj = service.request_evidence(
            request.user,
            audit,
            finding,
            **command(commands.FinanceEvidenceRequestSerializer, request),
        )
        return Response(version_data(obj), status=201)

    @action(
        detail=True,
        methods=["get"],
        url_path=r"transactions/(?P<item>[0-9a-f-]{36})/corrections",
    )
    def corrections(self, request, uuid=None, item=None):
        row = get_object_or_404(service.rows(self.audit()), uuid=item)
        return self.paginated(
            row.corrections.order_by("number"),
            lambda obj: {
                **version_data(obj),
                "original": obj.original,
                "corrected": obj.corrected,
                "actor": obj.actor_id,
                "reason": obj.reason,
            },
        )

    @action(
        detail=True,
        methods=["get"],
        url_path=r"transactions/(?P<item>[0-9a-f-]{36})/evidence",
    )
    def evidence(self, request, uuid=None, item=None):
        row = get_object_or_404(service.rows(self.audit()), uuid=item)
        return self.paginated(
            row.evidence_links.select_related("document").order_by("pk"),
            lambda obj: {
                "uuid": str(obj.uuid),
                "document_uuid": str(obj.document.uuid),
                "role": obj.role,
                "data": obj.data,
                "checksum": obj.checksum,
                "governance_revision": obj.governance_revision,
            },
        )

    @action(detail=True, methods=["get"], url_path=r"outputs/(?P<output>[0-9a-f-]{36})")
    def output(self, request, uuid=None, output=None):
        audit = self.audit()
        obj = get_object_or_404(audit.outputs, uuid=output)
        return Response({**version_data(obj), "data": obj.data})

    @action(
        detail=True,
        methods=["post"],
        url_path=r"outputs/(?P<output>[0-9a-f-]{36})/export",
    )
    def export(self, request, uuid=None, output=None):
        command(commands.StrictSerializer, request)
        audit = self.audit()
        obj = get_object_or_404(audit.outputs.select_related("run"), uuid=output)
        return Response(service.export(request.user, audit, obj))
