from django.db import transaction
from rest_framework.generics import get_object_or_404
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.documents.models import Document
from apps.workflows.serializers.rfp_serializers import (
    RFPCreateSerializer,
    RFPRevisionSerializer,
    RFPSubmitSerializer,
    RFPResponseSerializer,
    RFPAttachmentSerializer,
    RFPExportSerializer,
    RFPCorrectionSerializer,
)
from apps.workflows.serializers.workflow_serializers import WorkflowRunSerializer
from apps.workflows.services.rfp_service import RFPService, event
from apps.workflows.services.rfp_workflow_service import RFPWorkflowService
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import WorkflowConflict
from apps.workflows.views.clause_views import NoStoreViewSet, command


def project_data(project):
    return {
        "uuid": str(project.uuid),
        "title": project.title,
        "department": project.department_id,
        "status": project.status,
        "revision": project.revision,
        "extraction_reviewed": project.extraction_reviewed,
        "run_uuid": str(project.run.uuid) if project.run_id else None,
    }


def response_data(response, user):
    try:
        grounding = RFPService.revalidate(user, response)
    except PermissionDenied:
        return {
            "uuid": str(response.uuid),
            "number": response.number,
            "grounding": "evidence_invalid",
            "review_required": True,
            "text": None,
            "evidence": [],
        }
    return {
        "uuid": str(response.uuid),
        "number": response.number,
        "text": response.text,
        "origin": response.origin,
        "grounding": grounding,
        "evidence": response.evidence,
        "reason": response.reason,
        "actor": response.actor_id,
        "created_at": response.created_at,
        "externally_shareable": response.externally_shareable,
    }


def item_data(item, user):
    latest = next(iter(item.latest_responses), None)
    response = response_data(latest, user) if latest else None
    return {
        "uuid": str(item.uuid),
        "key": item.key,
        "section": item.section,
        "kind": item.kind,
        "category": item.category,
        "text": item.text,
        "mandatory": item.mandatory,
        "response_required": item.response_required,
        "evidence": item.evidence,
        "structure": item.structure,
        "assignment": item.assignment,
        "compliance": "review_required"
        if response and response["grounding"] == "evidence_invalid"
        else item.compliance,
        "revision": item.revision,
        "response": response,
    }


class RFPProjectViewSet(NoStoreViewSet):
    serializer_class = RFPCreateSerializer

    def get_queryset(self):
        return (
            RFPService.visible(self.request.user).select_related("run").order_by("-pk")
        )

    def project(self):
        return RFPService.authorize(self.request.user, self.get_object())

    def list(self, request):
        page = self.paginate_queryset(self.get_queryset())
        rows = []
        for project in page:
            try:
                RFPService.authorize(request.user, project)
            except PermissionDenied:
                continue
            rows.append(project_data(project))
        return self.get_paginated_response(rows)

    def retrieve(self, request, uuid=None):
        project = self.project()
        data = project_data(project)
        data["metadata"] = project.metadata
        data["finalization"] = project.configuration.get("finalization", {})
        data["knowledge_document_uuids"] = project.configuration[
            "knowledge_document_uuids"
        ]
        data["sources"] = [
            {
                "uuid": str(s.uuid),
                "document_uuid": str(s.document.uuid),
                "role": s.role,
                "source_digest": s.source_digest,
                "governance_revision": s.governance_revision,
            }
            for s in project.sources.select_related("document")
        ]
        return Response(data)

    def create(self, request):
        project = RFPService.create(
            request.user, **command(RFPCreateSerializer, request)
        )
        return Response(project_data(project), status=201)

    @action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        run = RFPWorkflowService.submit(
            request.user, self.project(), **command(RFPSubmitSerializer, request)
        )
        return Response(WorkflowRunSerializer(run, context={"request": request}).data)

    @action(detail=True, methods=["get"])
    def requirements(self, request, uuid=None):
        project = self.project()
        qs = RFPService.latest_items(project).order_by("pk")
        if request.query_params.get("kind"):
            qs = qs.filter(kind=request.query_params["kind"])
        page = self.paginate_queryset(qs)
        return self.get_paginated_response([item_data(i, request.user) for i in page])

    @action(
        detail=True, methods=["get"], url_path="responses/(?P<item_uuid>[^/.]+)/history"
    )
    def history(self, request, uuid=None, item_uuid=None):
        item = get_object_or_404(self.project().items, uuid=item_uuid)
        page = self.paginate_queryset(item.responses.order_by("-number"))
        return self.get_paginated_response(
            [response_data(r, request.user) for r in page]
        )

    @action(detail=True, methods=["post"], url_path="responses/(?P<item_uuid>[^/.]+)")
    def correct_response(self, request, uuid=None, item_uuid=None):
        project = self.project()
        item = get_object_or_404(project.items, uuid=item_uuid)
        response = RFPService.edit(
            request.user, project, item, **command(RFPResponseSerializer, request)
        )
        return Response(response_data(response, request.user), status=201)

    @action(detail=True, methods=["post"], url_path="confirm-extraction")
    @transaction.atomic
    def confirm_extraction(self, request, uuid=None):
        values = command(RFPRevisionSerializer, request)
        project = RFPService.locked(request.user, self.project(), values["revision"])
        RFPService.require_editor(request.user, project)
        if (
            not project.run_id
            or project.run.current_step.key != "review_proposal"
            or not project.metadata
        ):
            raise WorkflowConflict(
                "Extraction confirmation requires solicitation review."
            )
        # Confirmation acknowledges the source candidates; missing facts remain unknown.
        for source in project.sources.select_related("document"):
            deadlines = project.metadata.get(str(source.document.uuid), {}).get(
                "deadlines", []
            )
            for index, deadline in enumerate(deadlines):
                if (
                    deadline["review_required"]
                    and not project.corrections.filter(
                        source=source, kind="deadline", key=str(index)
                    ).exists()
                ):
                    raise ValidationError(
                        "An ambiguous deadline requires a validated source correction."
                    )
        project.extraction_reviewed = True
        project.save(update_fields=["extraction_reviewed", "updated_at"])
        RFPService.changed(project)
        event(request.user, project, "extraction_reviewed")
        return Response(project_data(project))

    @action(detail=True, methods=["get", "post"])
    def corrections(self, request, uuid=None):
        project = self.project()
        if request.method == "POST":
            correction = RFPService.correct_extraction(
                request.user, project, **command(RFPCorrectionSerializer, request)
            )
            return Response(
                {"uuid": str(correction.uuid), "number": correction.number}, status=201
            )
        page = self.paginate_queryset(
            project.corrections.select_related("source").order_by("-pk")
        )
        return self.get_paginated_response(
            [
                {
                    "uuid": str(c.uuid),
                    "source_uuid": str(c.source.uuid),
                    "kind": c.kind,
                    "key": c.key,
                    "number": c.number,
                    "value": c.value,
                    "evidence": c.evidence,
                    "actor": c.actor_id,
                    "reason": c.reason,
                    "created_at": c.created_at,
                }
                for c in page
            ]
        )

    @action(detail=True, methods=["post"], url_path="draft-batch")
    @transaction.atomic
    def draft_batch(self, request, uuid=None):
        values = command(RFPRevisionSerializer, request)
        project = RFPService.locked(request.user, self.project(), values["revision"])
        RFPService.require_editor(request.user, project)
        if not project.run_id or project.run.current_step.key != "review_proposal":
            raise WorkflowConflict("Additional drafting requires an active review.")
        step_run = project.run.step_runs.get(step__key="draft_responses")
        result = RFPService.draft(project, step_run)
        if result["drafted"]:
            RFPService.changed(project)
            event(request.user, project, "draft_batch_generated")
        return Response({**result, "revision": project.revision})

    @action(detail=True, methods=["get"])
    def attachments(self, request, uuid=None):
        page = self.paginate_queryset(
            self.project().attachments.select_related("document").order_by("pk")
        )
        return self.get_paginated_response(
            [
                {
                    "uuid": str(a.uuid),
                    "text": a.text,
                    "mandatory": a.mandatory,
                    "evidence": a.evidence,
                    "document_uuid": str(a.document.uuid) if a.document_id else None,
                    "reviewed": a.reviewed,
                    "revision": a.revision,
                    "assignment": a.assignment,
                }
                for a in page
            ]
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="attachments/(?P<attachment_uuid>[^/.]+)",
    )
    @transaction.atomic
    def attach(self, request, uuid=None, attachment_uuid=None):
        values = command(RFPAttachmentSerializer, request)
        project = RFPService.locked(request.user, self.project())
        RFPService.require_editor(request.user, project)
        if not project.run_id or project.run.current_step.key != "review_proposal":
            raise WorkflowConflict("Attachment changes require review.")
        attachment = get_object_or_404(
            project.attachments.select_for_update(), uuid=attachment_uuid
        )
        if attachment.revision != values["revision"]:
            raise WorkflowConflict()
        doc = get_object_or_404(
            Document, uuid=values["document_uuid"], company=request.user.company
        )
        RFPService.document(request.user, doc, "export")
        assignment = values.get("assignment", {})
        if assignment:
            WorkflowDefinitionService.validate_assignment(
                request.user.company_id, assignment
            )
        attachment.document, attachment.assignment, attachment.reviewed = (
            doc,
            assignment,
            True,
        )
        attachment.revision += 1
        attachment.save(
            update_fields=[
                "document",
                "assignment",
                "reviewed",
                "revision",
                "updated_at",
            ]
        )
        RFPService.changed(project)
        event(request.user, project, "attachment_reviewed")
        return Response({"revision": attachment.revision})

    @action(detail=True, methods=["get"])
    def outputs(self, request, uuid=None):
        project = self.project()
        page = self.paginate_queryset(project.outputs.order_by("-number"))
        return self.get_paginated_response(
            [
                {
                    "uuid": str(o.uuid),
                    "number": o.number,
                    "project_revision": o.project_revision,
                    "created_at": o.created_at,
                }
                for o in page
            ]
        )

    @action(
        detail=True, methods=["post"], url_path="outputs/(?P<output_uuid>[^/.]+)/export"
    )
    def export(self, request, uuid=None, output_uuid=None):
        project = self.project()
        output = get_object_or_404(project.outputs, uuid=output_uuid)
        return Response(
            RFPService.export(
                request.user, project, output, **command(RFPExportSerializer, request)
            )
        )
