from uuid import UUID

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workflows.models import ClauseAnalysis, ClausePlaybookVersion
from apps.workflows.services.clause_analysis_service import ClauseAnalysisService
from apps.workflows.services.clause_playbook_service import ClausePlaybookUnavailable
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_processing_service import ProcessingResult


def options(value):
    if (
        not isinstance(value, dict)
        or set(value) - {"playbook_version", "use_ai"}
        or type(value.get("use_ai", False)) is not bool
    ):
        raise ValidationError("Invalid legal clause processing options.")
    explicit = value.get("playbook_version")
    if explicit is not None:
        try:
            if not isinstance(explicit, str):
                raise ValueError
            UUID(explicit)
        except (ValueError, TypeError):
            raise ValidationError(
                "Legal processing requires a published playbook UUID."
            )
    return {"playbook_version": explicit, "use_ai": value.get("use_ai", False)}


def graph(policy, reviewer, approver):
    def processor(key):
        return {
            "key": key,
            "name": key.replace("_", " ").title(),
            "step_type": "processing",
            "configuration": {
                "adapter": "legal_clause_intelligence",
                "operation": key,
                "options": policy,
                "exception_assignment": reviewer,
            },
        }

    steps = [
        processor("analyze_clauses"),
        {
            "key": "review_clauses",
            "name": "Review legal clauses",
            "step_type": "human_review",
            "configuration": {"assignment": reviewer},
        },
        processor("prepare_clause_output"),
        {
            "key": "clause_approval_policy",
            "name": "Legal approval policy",
            "step_type": "rule",
            "configuration": {
                "source_step": "prepare_clause_output",
                "path": "approval_required",
                "operator": "eq",
                "value": True,
            },
        },
        {
            "key": "approve_clause_exceptions",
            "name": "Approve legal exceptions",
            "step_type": "approval",
            "configuration": {"assignment": approver},
        },
        processor("finalize_clause_analysis"),
        {"key": "completed", "name": "Completed", "step_type": "terminal"},
        {
            "key": "rejected",
            "name": "Rejected",
            "step_type": "terminal",
            "configuration": {"status": "rejected"},
        },
    ]
    edges = [
        ("analyze_clauses", "review_clauses", "success"),
        ("review_clauses", "prepare_clause_output", "accepted"),
        ("review_clauses", "prepare_clause_output", "exception"),
        ("review_clauses", "rejected", "rejected"),
        ("prepare_clause_output", "clause_approval_policy", "success"),
        ("clause_approval_policy", "approve_clause_exceptions", "pass"),
        ("clause_approval_policy", "finalize_clause_analysis", "fail"),
        ("approve_clause_exceptions", "finalize_clause_analysis", "approved"),
        ("approve_clause_exceptions", "rejected", "rejected"),
        ("finalize_clause_analysis", "completed", "success"),
    ]
    edges.append(("analyze_clauses", "review_clauses", "exception"))
    edges.append(("approve_clause_exceptions", "rejected", "exception"))
    return steps, [{"source": a, "target": b, "outcome": c} for a, b, c in edges]


class ClauseDefinitionService:
    @staticmethod
    @transaction.atomic
    def create(
        user, *, name, code, reviewer, approver=None, department=None, policy=None
    ):
        policy = options(policy or {})
        WorkflowDefinitionService.require_admin(user, department)
        WorkflowDefinitionService.validate_assignment(user.company_id, reviewer)
        approver = approver or reviewer
        WorkflowDefinitionService.validate_assignment(user.company_id, approver)
        if policy["playbook_version"]:
            from apps.workflows.services.clause_playbook_service import (
                ClausePlaybookService,
            )

            version = ClausePlaybookVersion.objects.filter(
                uuid=policy["playbook_version"],
                playbook__in=ClausePlaybookService.visible(user),
            ).first()
            if not version or version.playbook.department_id not in (None, department):
                raise PermissionDenied(
                    "The selected legal playbook is unavailable in this scope."
                )
        definition = WorkflowDefinitionService.create(
            user,
            name=name,
            code=code,
            workflow_type="legal_clause_intelligence",
            description="Source-grounded legal clause intelligence with organization policy review.",
            department_id=department,
        )
        steps, transitions = graph(policy, reviewer, approver)
        version = WorkflowDefinitionService.publish(
            user,
            definition,
            entry_step="analyze_clauses",
            steps=steps,
            transitions=transitions,
            allowed_document_types=[],
            configuration={"max_attempts": 4},
        )
        return definition, version

    @staticmethod
    def validate_version(version, policy):
        actual = {s.key: s for s in version.steps.all()}
        review, approval = (
            actual.get("review_clauses"),
            actual.get("approve_clause_exceptions"),
        )
        if not review or not approval:
            raise ValidationError(
                "Legal workflow requires review and approval boundaries."
            )
        expected, edges = graph(
            options(policy),
            review.configuration.get("assignment"),
            approval.configuration.get("assignment"),
        )
        if version.entry_step != "analyze_clauses" or set(actual) != {
            s["key"] for s in expected
        }:
            raise ValidationError("Legal lifecycle cannot be bypassed.")
        if any(
            actual[s["key"]].step_type != s["step_type"]
            or actual[s["key"]].configuration != s.get("configuration", {})
            for s in expected
        ):
            raise ValidationError("Legal lifecycle policy cannot be bypassed.")
        if set(
            version.transitions.values_list("source__key", "target__key", "outcome")
        ) != {(e["source"], e["target"], e["outcome"]) for e in edges}:
            raise ValidationError("Legal lifecycle transitions cannot be bypassed.")


class ClauseProcessingAdapter:
    validate_version = staticmethod(ClauseDefinitionService.validate_version)

    @staticmethod
    def snapshot(operation, policy):
        return {"schema_version": 1}

    @staticmethod
    def validate_config(company_id, operation, policy):
        if operation not in (
            "analyze_clauses",
            "prepare_clause_output",
            "finalize_clause_analysis",
        ):
            raise ValidationError("Unsupported legal processing operation.")
        validated = options(policy)
        if (
            validated["playbook_version"]
            and not ClausePlaybookVersion.objects.filter(
                uuid=validated["playbook_version"],
                playbook__company_id=company_id,
                is_deleted=False,
                playbook__is_deleted=False,
            ).exists()
        ):
            raise ValidationError("The published legal playbook is unavailable.")

    @staticmethod
    def validate_start(user, documents, policy):
        if len(documents) != 1:
            raise ValidationError(
                "Legal analysis requires one principal source document."
            )
        document = documents[0]
        DocumentPermissionService.require_action(user, document, "extract")
        if any(
            FieldPermissionService.has_restrictions(user, document, a)
            for a in ("view", "ai")
        ):
            raise PermissionDenied("Current field policies deny legal processing.")
        if options(policy)["use_ai"]:
            DocumentPermissionService.require_action(user, document, "ai")

    @staticmethod
    def validate_command(user, run, action, policy):
        analysis = ClauseAnalysis.objects.filter(workflow_run=run).first()
        if not analysis and action in (
            "accept",
            "approve",
            "approved",
            "resolve_exception",
        ):
            raise ValidationError(
                "Create a new legal analysis after resolving the source or playbook configuration."
            )
        if analysis and (
            action in ("accept", "approve", "approved")
            or action == "resolve_exception"
            and run.current_step.key == "review_clauses"
        ):
            ClauseAnalysisService.assert_review(analysis)

    @staticmethod
    def execute(run, step_run, operation, policy):
        policy = options(policy)
        if operation == "analyze_clauses":
            try:
                analysis = ClauseAnalysisService.analyze(run, step_run, policy)
            except ClausePlaybookUnavailable:
                return ProcessingResult(
                    {"review_required": True}, exception_category="playbook_unavailable"
                )
            except ValidationError:
                return ProcessingResult(
                    {"review_required": True}, exception_category="evidence_invalid"
                )
            category = (
                "coverage_insufficient"
                if not analysis.coverage.get("complete")
                else "clause_ambiguous"
                if analysis.deviations.filter(
                    revision=analysis.revision, comparison="ambiguous"
                ).exists()
                else None
            )
            return ProcessingResult(
                {"analysis_uuid": str(analysis.uuid), "revision": analysis.revision},
                exception_category=category,
            )
        analysis = ClauseAnalysis.objects.get(workflow_run=run)
        ClauseAnalysisService.assert_review(analysis)
        if operation == "prepare_clause_output":
            return ProcessingResult(
                {
                    "analysis_uuid": str(analysis.uuid),
                    "approval_required": ClauseAnalysisService.approval_required(
                        analysis
                    ),
                }
            )
        ClauseAnalysisService.finalize(analysis)
        output = analysis.outputs.get(revision=analysis.revision)
        return ProcessingResult(
            {
                "analysis_uuid": str(analysis.uuid),
                "output_uuid": str(output.uuid),
                "digest": output.digest,
            }
        )
