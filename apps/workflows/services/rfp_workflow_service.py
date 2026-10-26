from uuid import UUID, uuid4

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.workflows.models import RFPProject
from apps.workflows.services.rfp_service import RFPService
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowEngineService,
    WorkflowConflict,
)
from apps.workflows.services.workflow_processing_service import ProcessingResult


def options(value):
    if not isinstance(value, dict) or set(value) != {"project_uuid"}:
        raise ValidationError("An RFP project UUID is required.")
    try:
        return {"project_uuid": str(UUID(str(value["project_uuid"])))}
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Invalid RFP project UUID.")


def graph(project, reviewer, approver):
    def processor(key):
        return {
            "key": key,
            "name": key.replace("_", " ").title(),
            "step_type": "processing",
            "configuration": {
                "adapter": "rfp_proposal",
                "operation": key,
                "options": project,
                "exception_assignment": reviewer,
            },
        }

    steps = [
        processor("ingest_rfp"),
        processor("draft_responses"),
        {
            "key": "review_proposal",
            "name": "Review solicitation, responses and evidence",
            "step_type": "human_review",
            "configuration": {"assignment": reviewer},
        },
        processor("validate_proposal"),
        {
            "key": "approve_proposal",
            "name": "Approve response versions and disclosure",
            "step_type": "approval",
            "configuration": {"assignment": approver},
        },
        processor("assemble_proposal"),
        {"key": "completed", "name": "Completed", "step_type": "terminal"},
        {
            "key": "rejected",
            "name": "Rejected",
            "step_type": "terminal",
            "configuration": {"status": "rejected"},
        },
    ]
    edges = [
        ("ingest_rfp", "draft_responses", "success"),
        ("ingest_rfp", "review_proposal", "exception"),
        ("draft_responses", "review_proposal", "success"),
        ("draft_responses", "review_proposal", "exception"),
        ("review_proposal", "validate_proposal", "accepted"),
        ("review_proposal", "rejected", "rejected"),
        ("review_proposal", "rejected", "exception"),
        ("validate_proposal", "approve_proposal", "success"),
        ("validate_proposal", "rejected", "exception"),
        ("approve_proposal", "assemble_proposal", "approved"),
        ("approve_proposal", "rejected", "rejected"),
        ("approve_proposal", "rejected", "exception"),
        ("assemble_proposal", "completed", "success"),
        ("assemble_proposal", "rejected", "exception"),
    ]
    return steps, [{"source": a, "target": b, "outcome": c} for a, b, c in edges]


class RFPWorkflowService:
    @classmethod
    def submit(cls, user, project, **values):
        # A simultaneous first start may attach a run while another caller waits
        # for the company lock. Retry in a fresh transaction and take run first.
        for attempt in range(2):
            try:
                return cls._submit(user, project, **values)
            except WorkflowConflict:
                if attempt:
                    raise

    @staticmethod
    @transaction.atomic
    def _submit(user, project, *, revision, reviewer, approver, idempotency_key):
        project = RFPService.locked(user, project)
        WorkflowDefinitionService.require_admin(user, project.department_id)
        if project.run_id:
            run = WorkflowEngineService.locked(project.run)
            if run.idempotency_key == idempotency_key:
                if (
                    run.metadata.get("start_revision") != revision
                    or run.metadata.get("reviewer") != reviewer
                    or run.metadata.get("approver") != approver
                ):
                    raise WorkflowConflict()
                return run
            if run.status not in WorkflowEngineService.CLOSED:
                raise WorkflowConflict("RFP project already has an active workflow.")
        if project.revision != revision:
            raise WorkflowConflict()
        RFPService.authorize(user, project, "ai")
        WorkflowDefinitionService.validate_assignment(user.company_id, reviewer)
        WorkflowDefinitionService.validate_assignment(user.company_id, approver)
        definition = WorkflowDefinitionService.create(
            user,
            name=project.title[:150],
            code="rfp-" + project.uuid.hex + "-" + uuid4().hex[:8],
            workflow_type="rfp_proposal",
            department_id=project.department_id,
        )
        steps, transitions = graph(
            {"project_uuid": str(project.uuid)}, reviewer, approver
        )
        version = WorkflowDefinitionService.publish(
            user,
            definition,
            entry_step="ingest_rfp",
            steps=steps,
            transitions=transitions,
            allowed_document_types=[],
            configuration={"max_attempts": 4},
        )
        run = WorkflowEngineService.start(
            user,
            version_uuid=version.uuid,
            document_uuids=[
                str(s.document.uuid) for s in project.sources.select_related("document")
            ],
            idempotency_key=idempotency_key,
            metadata={
                "rfp_project_uuid": str(project.uuid),
                "project_revision": revision,
                "start_revision": revision,
                "reviewer": reviewer,
                "approver": approver,
            },
        )[0]
        project.run = run
        project.status = "ingesting"
        project.save(update_fields=["run", "status", "updated_at"])
        return run


class RFPProcessingAdapter:
    @staticmethod
    def snapshot(operation, value):
        return {"schema_version": 1}

    @staticmethod
    def validate_config(company_id, operation, value):
        if (
            operation
            not in {
                "ingest_rfp",
                "draft_responses",
                "validate_proposal",
                "assemble_proposal",
            }
            or not RFPProject.objects.filter(
                uuid=options(value)["project_uuid"],
                company_id=company_id,
                is_deleted=False,
            ).exists()
        ):
            raise ValidationError("RFP processing configuration is unavailable.")

    @staticmethod
    def validate_version(version, value):
        actual = {s.key: s for s in version.steps.all()}
        if "review_proposal" not in actual or "approve_proposal" not in actual:
            raise ValidationError("RFP proposals require review and approval.")
        steps, transitions = graph(
            options(value),
            actual["review_proposal"].configuration.get("assignment"),
            actual["approve_proposal"].configuration.get("assignment"),
        )
        if (
            version.entry_step != "ingest_rfp"
            or set(actual) != {s["key"] for s in steps}
            or any(
                actual[s["key"]].step_type != s["step_type"]
                or actual[s["key"]].configuration != s.get("configuration", {})
                for s in steps
            )
        ):
            raise ValidationError("RFP lifecycle cannot be bypassed.")
        if set(
            version.transitions.values_list("source__key", "target__key", "outcome")
        ) != {(e["source"], e["target"], e["outcome"]) for e in transitions}:
            raise ValidationError("RFP lifecycle transitions cannot be bypassed.")

    @staticmethod
    def validate_start(user, documents, value):
        project = (
            RFPService.visible(user).filter(uuid=options(value)["project_uuid"]).first()
        )
        if not project:
            raise PermissionDenied("RFP project is unavailable.")
        WorkflowDefinitionService.require_admin(user, project.department_id)
        RFPService.authorize(user, project, "ai")
        if (
            project.run_id and project.run.status not in WorkflowEngineService.CLOSED
        ) or {d.pk for d in documents} != set(
            project.sources.values_list("document_id", flat=True)
        ):
            raise ValidationError(
                "RFP workflow requires its exact pinned solicitation package."
            )

    @staticmethod
    def project(run, value):
        project = RFPProject.objects.get(
            uuid=options(value)["project_uuid"],
            company_id=run.company_id,
            is_deleted=False,
        )
        if project.run_id != run.pk or project.revision != run.metadata.get(
            "project_revision"
        ):
            raise WorkflowConflict("RFP workflow revision is stale.")
        if set(run.documents.values_list("pk", flat=True)) != set(
            project.sources.values_list("document_id", flat=True)
        ):
            raise WorkflowConflict("Solicitation package changed.")
        return project

    @classmethod
    def validate_command(cls, user, run, action, value):
        project = cls.project(run, value)
        RFPService.authorize(user, project)
        if action in {"accept", "approve", "approved"}:
            RFPService.readiness(user, project)

    @classmethod
    def execute(cls, run, step_run, operation, value):
        project = cls.project(run, value)
        RFPService.authorize(run.initiated_by, project)
        if operation == "ingest_rfp":
            try:
                result = RFPService.ingest(project, step_run)
            except ValidationError:
                return ProcessingResult(
                    {"review_required": True},
                    exception_category="requirement_extraction_failed",
                )
            return ProcessingResult(
                result,
                exception_category=None
                if result.get("classified")
                else "rfp_classification_uncertain",
            )
        if operation == "draft_responses":
            try:
                return ProcessingResult(RFPService.draft(project, step_run))
            except (RuntimeError, TimeoutError):
                return ProcessingResult(
                    {"review_required": True}, exception_category="provider_unavailable"
                )
            except ValidationError:
                return ProcessingResult(
                    {"review_required": True},
                    exception_category="response_generation_failed",
                )
        if operation == "validate_proposal":
            RFPService.readiness(run.initiated_by, project)
            project.status = "approval"
            project.save(update_fields=["status", "updated_at"])
            return ProcessingResult({"project_revision": project.revision})
        output = RFPService.assemble(project)
        return ProcessingResult(
            {"output_uuid": str(output.uuid), "number": output.number}
        )
