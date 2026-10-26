from uuid import UUID, uuid4

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.workflows.models.compliance import ComplianceAssessment
from apps.workflows.services.compliance_assessment_service import (
    ComplianceAssessmentService as service,
)
from apps.workflows.services.compliance_evidence_service import (
    ComplianceEvidenceService,
)
from apps.workflows.services import compliance_schema as schema
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowEngineService,
    WorkflowConflict,
)
from apps.workflows.services.workflow_processing_service import ProcessingResult


def options(value):
    schema.obj(value, {"assessment_uuid"}, {"assessment_uuid"})
    return {"assessment_uuid": schema.uuid(value["assessment_uuid"])}


def graph(project, reviewer, approver):
    def processor(key):
        return {
            "key": key,
            "name": key.replace("_", " ").title(),
            "step_type": "processing",
            "configuration": {
                "adapter": "compliance",
                "operation": key,
                "options": project,
                "exception_assignment": reviewer,
            },
        }

    steps = [
        processor("validate_scope"),
        processor("evaluate_controls"),
        {
            "key": "review_assessment",
            "name": "Review applicability, controls and evidence",
            "step_type": "human_review",
            "configuration": {"assignment": reviewer},
        },
        processor("validate_assessment"),
        {
            "key": "approve_assessment",
            "name": "Approve compliance evidence assessment",
            "step_type": "approval",
            "configuration": {"assignment": approver},
        },
        processor("finalize_assessment"),
        {"key": "completed", "name": "Completed", "step_type": "terminal"},
        {
            "key": "rejected",
            "name": "Rejected",
            "step_type": "terminal",
            "configuration": {"status": "rejected"},
        },
    ]
    edges = [
        ("validate_scope", "evaluate_controls", "success"),
        ("validate_scope", "review_assessment", "exception"),
        ("evaluate_controls", "review_assessment", "success"),
        ("evaluate_controls", "review_assessment", "exception"),
        ("review_assessment", "validate_assessment", "accepted"),
        ("review_assessment", "rejected", "rejected"),
        ("review_assessment", "rejected", "exception"),
        ("validate_assessment", "approve_assessment", "success"),
        ("validate_assessment", "rejected", "exception"),
        ("approve_assessment", "finalize_assessment", "approved"),
        ("approve_assessment", "rejected", "rejected"),
        ("approve_assessment", "rejected", "exception"),
        ("finalize_assessment", "completed", "success"),
        ("finalize_assessment", "rejected", "exception"),
    ]
    return steps, [{"source": a, "target": b, "outcome": c} for a, b, c in edges]


class ComplianceWorkflowService:
    @classmethod
    def submit(cls, user, assessment, **values):
        for attempt in range(2):
            try:
                return cls._submit(user, assessment, **values)
            except WorkflowConflict:
                if attempt:
                    raise

    @staticmethod
    @transaction.atomic
    def _submit(user, assessment, *, revision, reviewer, approver, idempotency_key):
        assessment = service.locked(user, assessment)
        WorkflowDefinitionService.require_admin(user, assessment.department_id)
        schema.text(idempotency_key, 128)
        if assessment.run_id:
            run = assessment.run
            if (
                run.idempotency_key == idempotency_key
                and run.metadata.get("start_revision") == revision
                and run.metadata.get("reviewer") == reviewer
                and run.metadata.get("approver") == approver
            ):
                return run
            raise WorkflowConflict(
                "Assessments have one workflow. Create a reassessment for new runs."
            )
        if revision != assessment.revision:
            raise WorkflowConflict()
        WorkflowDefinitionService.validate_assignment(user.company_id, reviewer)
        WorkflowDefinitionService.validate_assignment(user.company_id, approver)
        definition = WorkflowDefinitionService.create(
            user,
            name=assessment.title[:150],
            code="compliance-" + assessment.uuid.hex + "-" + uuid4().hex[:8],
            workflow_type="compliance",
            department_id=assessment.department_id,
        )
        steps, transitions = graph(
            {"assessment_uuid": str(assessment.uuid)}, reviewer, approver
        )
        version = WorkflowDefinitionService.publish(
            user,
            definition,
            entry_step="validate_scope",
            steps=steps,
            transitions=transitions,
            allowed_document_types=[],
            configuration={"max_attempts": 3},
        )
        run = WorkflowEngineService.start(
            user,
            version_uuid=version.uuid,
            document_uuids=assessment.scope["document_uuids"],
            idempotency_key=idempotency_key,
            metadata={
                "compliance_assessment_uuid": str(assessment.uuid),
                "assessment_revision": revision,
                "start_revision": revision,
                "reviewer": reviewer,
                "approver": approver,
            },
        )[0]
        assessment.run = run
        assessment.save(update_fields=["run", "updated_at"])
        return run


class ComplianceProcessingAdapter:
    @staticmethod
    def snapshot(operation, value):
        return {"schema_version": 1, "deterministic": True}

    @staticmethod
    def validate_config(company_id, operation, value):
        if (
            operation
            not in {
                "validate_scope",
                "evaluate_controls",
                "validate_assessment",
                "finalize_assessment",
            }
            or not ComplianceAssessment.objects.filter(
                company_id=company_id,
                uuid=options(value)["assessment_uuid"],
                is_deleted=False,
            ).exists()
        ):
            raise ValidationError("Compliance processing configuration is unavailable.")

    @staticmethod
    def validate_version(version, value):
        actual = {s.key: s for s in version.steps.all()}
        if "review_assessment" not in actual or "approve_assessment" not in actual:
            raise ValidationError("Compliance assessments require review and approval.")
        steps, transitions = graph(
            options(value),
            actual["review_assessment"].configuration.get("assignment"),
            actual["approve_assessment"].configuration.get("assignment"),
        )
        if (
            version.entry_step != "validate_scope"
            or set(actual) != {s["key"] for s in steps}
            or any(
                actual[s["key"]].step_type != s["step_type"]
                or actual[s["key"]].configuration != s.get("configuration", {})
                for s in steps
            )
            or set(
                version.transitions.values_list("source__key", "target__key", "outcome")
            )
            != {(e["source"], e["target"], e["outcome"]) for e in transitions}
        ):
            raise ValidationError(
                "Compliance review and approval lifecycle cannot be bypassed."
            )

    @staticmethod
    def validate_start(user, documents, value):
        assessment = (
            service.visible(user).filter(uuid=options(value)["assessment_uuid"]).first()
        )
        if not assessment:
            raise PermissionDenied("Compliance assessment unavailable.")
        WorkflowDefinitionService.require_admin(user, assessment.department_id)
        service.authorize(user, assessment)
        if assessment.run_id or {str(d.uuid) for d in documents} != set(
            assessment.scope["document_uuids"]
        ):
            raise ValidationError(
                "An assessment requires its exact pinned source scope and one workflow."
            )

    @staticmethod
    def project(run, value):
        assessment = ComplianceAssessment.objects.filter(
            uuid=options(value)["assessment_uuid"],
            company_id=run.company_id,
            is_deleted=False,
        ).first()
        if (
            not assessment
            or assessment.run_id != run.pk
            or assessment.revision != run.metadata.get("assessment_revision")
            or set(run.documents.values_list("uuid", flat=True))
            != {UUID(v) for v in assessment.scope["document_uuids"]}
        ):
            raise WorkflowConflict(
                "Compliance workflow source scope or revision changed."
            )
        return assessment

    @classmethod
    def validate_command(cls, user, run, action, value):
        assessment = cls.project(run, value)
        service.authorize(user, assessment)
        if action in {"accept", "approve", "approved"}:
            service.readiness(user, assessment)

    @classmethod
    def execute(cls, run, step_run, operation, value):
        assessment = cls.project(run, value)
        service.authorize(run.initiated_by, assessment)
        if operation == "validate_scope":
            ComplianceEvidenceService.documents(run.initiated_by, assessment.scope)
            return ProcessingResult(
                {"framework_version_uuid": str(assessment.framework_version.uuid)}
            )
        if operation == "evaluate_controls":
            evaluation = service.evaluate(
                run.initiated_by,
                assessment,
                revision=assessment.revision,
                idempotency_key="initial-" + str(run.uuid),
                internal=True,
            )
            run.refresh_from_db()
            return ProcessingResult(
                {
                    "evaluation_uuid": str(evaluation.uuid),
                    "counts": evaluation.data["counts"],
                }
            )
        if operation == "validate_assessment":
            evaluation = service.readiness(run.initiated_by, assessment)
            return ProcessingResult({"evaluation_uuid": str(evaluation.uuid)})
        output = service.assemble(assessment)
        return ProcessingResult(
            {"output_uuid": str(output.uuid), "digest": output.digest}
        )
