from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from collections import deque

from apps.workflows.models.source_proof import EvidenceRecord, ProofClaim
from apps.workflows.serializers import source_proof_serializers as commands
from apps.workflows.services.source_proof_service import (
    SourceProofService as proof,
    event,
)
from apps.workflows.services.proof_normalization_service import (
    ProofNormalizationService,
)
from apps.workflows.services.proof_source_service import ProofSourceService
from apps.workflows.views.clause_views import NoStoreViewSet, command


class EvidenceViewSet(NoStoreViewSet):
    serializer_class = commands.EvidenceCaptureSerializer

    def get_queryset(self):
        return (
            EvidenceRecord.objects.filter(
                source__company_id=self.request.user.company_id, is_deleted=False
            )
            .select_related("source")
            .order_by("pk")
        )

    def retrieve(self, request, uuid=None):
        return Response(
            proof.evidence_data(ProofSourceService(request.user), self.get_object())
        )

    @action(detail=True, methods=["get"])
    def citation(self, request, uuid=None):
        row = self.get_object()
        resolver = ProofSourceService(request.user)
        result = proof.evidence_data(resolver, row)
        resolved, _ = proof.source(resolver, row.source)
        result["documents"] = [
            {"uuid": str(doc.uuid), "title": doc.title} for doc in resolved["documents"]
        ]
        result["excerpt"] = None
        # Never quote text from a changed, rejected, expired or invalid locator.
        if result["validity"] == "valid":
            if row.locator_type == "text_offsets":
                doc = next(
                    d
                    for d in resolved["documents"]
                    if str(d.uuid) == row.locator["document_uuid"]
                )
                result["excerpt"] = resolver.document(doc)[1][
                    row.locator["start"] : row.locator["end"]
                ][:8000]
            elif row.locator_type == "chunk":
                from apps.vector_search.models import DocumentChunk

                result["excerpt"] = DocumentChunk.objects.get(
                    uuid=row.locator["chunk_uuid"],
                    document__uuid=row.locator["document_uuid"],
                    company=request.user.company,
                    is_deleted=False,
                ).content[:8000]
        return Response(result)

    def create(self, request):
        row = proof.capture(
            request.user, **command(commands.EvidenceCaptureSerializer, request)
        )
        return Response(
            proof.evidence_data(ProofSourceService(request.user), row), status=201
        )

    @action(detail=True, methods=["post"])
    def validate(self, request, uuid=None):
        row = self.get_object()
        # Authorization must precede validation; revoked content is not returned.
        proof.source(ProofSourceService(request.user), row.source)
        proof.validate(request.user, row)
        return self.retrieve(request, uuid)

    @action(detail=True, methods=["post"])
    def review(self, request, uuid=None):
        row = self.get_object()
        proof.review(
            request.user, row, **command(commands.ProofReviewSerializer, request)
        )
        return self.retrieve(request, uuid)

    @action(detail=True, methods=["get"])
    def history(self, request, uuid=None):
        row = self.get_object()
        proof.evidence_data(ProofSourceService(request.user), row)
        rows = self.paginate_queryset(row.validations.all())
        return self.get_paginated_response(
            [
                {
                    "number": v.number,
                    "status": v.status,
                    "code": v.code,
                    "actor": v.actor_id,
                    "created_at": v.created_at,
                }
                for v in rows
            ]
        )

    @action(detail=True, methods=["get"])
    def reviews(self, request, uuid=None):
        row = self.get_object()
        proof.evidence_data(ProofSourceService(request.user), row)
        return self.get_paginated_response(
            [
                {
                    "uuid": str(v.uuid),
                    "number": v.number,
                    "decision": v.decision,
                    "reason": v.reason,
                    "actor": v.actor_id,
                    "created_at": v.created_at,
                }
                for v in self.paginate_queryset(row.reviews.all())
            ]
        )


class ProofClaimViewSet(NoStoreViewSet):
    serializer_class = commands.ProofOutputSerializer

    def get_queryset(self):
        return (
            ProofClaim.objects.filter(
                company_id=self.request.user.company_id, is_deleted=False
            )
            .select_related("origin")
            .order_by("pk")
        )

    def retrieve(self, request, uuid=None):
        return Response(
            proof.graph(ProofSourceService(request.user), self.get_object())
        )

    @action(detail=False, methods=["get"])
    def context(self, request):
        """Discover stored proof without creating claims or changing domain state."""
        from apps.documents.models import Document
        from apps.workflows.models import (
            WorkflowRun,
            HRPolicyVersion,
            WorkflowArtifact,
            FinanceDataset,
            FinanceTransaction,
            RFPResponseVersion,
        )
        from apps.workflows.permissions.workflow_permissions import (
            WorkflowPermissionService,
        )
        from apps.workflows.services.proof_source_service import OUTPUTS

        class ContextInput(serializers.Serializer):
            run_uuid = serializers.UUIDField(required=False)
            document_uuid = serializers.UUIDField(required=False)
            page = serializers.IntegerField(min_value=1, max_value=1000, default=1)

        form = ContextInput(data=request.query_params)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        if bool(data.get("run_uuid")) == bool(data.get("document_uuid")):
            raise ValidationError("Choose exactly one proof context.")
        resolver = ProofSourceService(request.user)
        output_fields = []
        if data.get("run_uuid"):
            run = WorkflowPermissionService.visible_runs(
                request.user, WorkflowRun.objects.filter(uuid=data["run_uuid"])
            ).first()
            if not run or not WorkflowPermissionService.content_allowed(
                request.user, run
            ):
                raise PermissionDenied("Workflow proof is unavailable.")
            WorkflowPermissionService.require(request.user, run, "view")
            sources = Q(kind="document", reference__in=run.documents.values("uuid"))
            for kind, (model, relation, run_field) in OUTPUTS.items():
                outputs = model.objects.filter(
                    **{relation + "__" + run_field: run}, is_deleted=False
                )
                sources |= Q(kind=kind, reference__in=outputs.values("uuid"))
                # Offer only native paths from currently authorized immutable outputs.
                # Creating canonical proof remains an explicit POST, never a GET side effect.
                for output in outputs.order_by("-pk")[:5]:
                    try:
                        resolved = resolver.resolve(kind, output.uuid)
                    except (PermissionDenied, ValidationError):
                        continue
                    stack = deque([(resolved["data"], [])])
                    visited = 0
                    while stack and len(output_fields) < 100 and visited < 500:
                        value, path = stack.popleft()
                        visited += 1
                        if len(path) > 8:
                            continue
                        if isinstance(value, dict):
                            stack.extend(
                                (v, path + [k]) for k, v in list(value.items())[:100]
                            )
                        elif isinstance(value, list):
                            stack.extend(
                                (v, path + [i]) for i, v in enumerate(value[:100])
                            )
                        elif path:
                            output_fields.append(
                                {
                                    "kind": kind,
                                    "source_uuid": str(output.uuid),
                                    "path": path,
                                }
                            )
            for kind, model, relation in (
                ("hr_policy_version", HRPolicyVersion, "workflow_run"),
                ("workflow_artifact", WorkflowArtifact, "run"),
                ("finance_dataset", FinanceDataset, "audit__run"),
                ("finance_transaction", FinanceTransaction, "dataset__audit__run"),
                ("rfp_response", RFPResponseVersion, "item__project__run"),
            ):
                sources |= Q(
                    kind=kind,
                    reference__in=model.objects.filter(
                        **{relation: run}, is_deleted=False
                    ).values("uuid"),
                )
        else:
            doc = Document.objects.filter(
                uuid=data["document_uuid"],
                company=request.user.company,
                is_deleted=False,
            ).first()
            if not doc:
                raise PermissionDenied("Document proof is unavailable.")
            resolver.document(doc)
            sources = Q(documents=doc)
        from apps.workflows.models.source_proof import EvidenceSource

        source_ids = EvidenceSource.objects.filter(
            sources, company=request.user.company, is_deleted=False
        ).values("pk")
        page = data["page"]
        start, end = (page - 1) * 25, page * 25
        claims = list(
            self.get_queryset()
            .filter(origin_id__in=source_ids)
            .order_by("pk")[start : end + 1]
        )
        evidence = list(
            EvidenceRecord.objects.filter(source_id__in=source_ids, is_deleted=False)
            .select_related("source")
            .order_by("pk")[start : end + 1]
        )
        claim_rows, evidence_rows = [], []
        for claim in claims[:25]:
            try:
                graph = proof.graph(resolver, claim)
            except (PermissionDenied, ValidationError):
                continue
            current = next(
                c
                for c in graph["claims"]
                if c["claim_uuid"] == str(claim.uuid)
                and c["revision"] == graph["current_revision"]
            )
            claim_rows.append(
                {
                    "uuid": str(claim.uuid),
                    "key": claim.key,
                    "domain": claim.domain,
                    "value": current["value"],
                    "revision": graph["current_revision"],
                    "coverage": graph["coverage"],
                }
            )
        for row in evidence[:25]:
            try:
                evidence_rows.append(proof.evidence_data(resolver, row))
            except (PermissionDenied, ValidationError):
                continue
        return Response(
            {
                "claims": claim_rows,
                "evidence": evidence_rows,
                "output_fields": output_fields,
                "next": len(claims) > 25 or len(evidence) > 25,
                "previous": page > 1,
            }
        )

    @action(detail=False, methods=["post"], url_path="output")
    def output(self, request):
        claim = ProofNormalizationService.output(
            request.user, **command(commands.ProofOutputSerializer, request)
        )
        result = proof.graph(ProofSourceService(request.user), claim)
        return Response(result)

    @action(detail=False, methods=["post"], url_path="internal-answer")
    def internal_answer(self, request):
        claim = ProofNormalizationService.message(
            request.user, **command(commands.ProofMessageSerializer, request)
        )
        return Response(proof.graph(ProofSourceService(request.user), claim))

    @action(detail=True, methods=["post"])
    def correct(self, request, uuid=None):
        claim = proof.correct(
            request.user,
            self.get_object(),
            **command(commands.ProofCorrectionSerializer, request),
        )
        return Response(proof.graph(ProofSourceService(request.user), claim))

    @action(detail=True, methods=["post"])
    def export(self, request, uuid=None):
        return Response(
            proof.export(
                request.user,
                self.get_object(),
                **command(commands.ProofExportSerializer, request),
            )
        )

    @action(detail=True, methods=["get"])
    def package(self, request, uuid=None):
        claim = self.get_object()
        result = proof.graph(ProofSourceService(request.user), claim)
        event(request.user, claim, "proof_generated")
        return Response(result)
