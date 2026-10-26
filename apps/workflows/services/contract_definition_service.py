from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.workflows.services.contract_schema import ContractPolicy
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)

OPERATIONS = (
    "validate_document",
    "classify_contract",
    "extract_contract_metadata",
    "extract_parties",
    "extract_key_dates",
    "extract_commercial_terms",
    "extract_obligations",
    "identify_core_clauses",
    "assess_contract_risks",
    "detect_missing_information",
    "validate_evidence",
    "assess_confidence",
)
FINAL_OPERATION = "finalize_contract_review"


def graph(policy, reviewer, approver):
    def processor(key):
        return {
            "key": key,
            "name": key.replace("_", " ").title(),
            "step_type": "processing",
            "configuration": {
                "adapter": "contract_review",
                "operation": key,
                "options": policy,
                "exception_assignment": reviewer,
            },
        }

    steps = [processor(key) for key in OPERATIONS]
    steps += [
        {
            "key": "review_decision",
            "name": "Review policy",
            "step_type": "rule",
            "configuration": {
                "source_step": "assess_confidence",
                "path": "review_required",
                "operator": "eq",
                "value": True,
            },
        },
        {
            "key": "review_if_required",
            "name": "Review contract",
            "step_type": "human_review",
            "configuration": {"assignment": reviewer},
        },
        processor("prepare_reviewed_output"),
        {
            "key": "approval_decision",
            "name": "Approval policy",
            "step_type": "rule",
            "configuration": {
                "source_step": "prepare_reviewed_output",
                "path": "approval_required",
                "operator": "eq",
                "value": True,
            },
        },
        {
            "key": "approve_if_required",
            "name": "Approve contract",
            "step_type": "approval",
            "configuration": {"assignment": approver},
        },
        processor(FINAL_OPERATION),
        {"key": "completed", "name": "Completed", "step_type": "terminal"},
        {
            "key": "rejected",
            "name": "Rejected",
            "step_type": "terminal",
            "configuration": {"status": "rejected"},
        },
    ]
    edges = [(a, b, "success") for a, b in zip(OPERATIONS, OPERATIONS[1:])]
    edges += [
        ("assess_confidence", "review_decision", "success"),
        ("review_decision", "review_if_required", "pass"),
        ("review_decision", "prepare_reviewed_output", "fail"),
        ("review_if_required", "prepare_reviewed_output", "accepted"),
        ("review_if_required", "prepare_reviewed_output", "exception"),
        ("review_if_required", "rejected", "rejected"),
        ("prepare_reviewed_output", "approval_decision", "success"),
        ("approval_decision", "approve_if_required", "pass"),
        ("approval_decision", FINAL_OPERATION, "fail"),
        ("approve_if_required", FINAL_OPERATION, "approved"),
        ("approve_if_required", "rejected", "rejected"),
        (FINAL_OPERATION, "completed", "success"),
    ]
    return steps, [{"source": a, "target": b, "outcome": c} for a, b, c in edges]


class ContractDefinitionService:
    @staticmethod
    @transaction.atomic
    def create(
        user, *, name, code, reviewer, approver=None, department=None, policy=None
    ):
        policy = ContractPolicy.validate(policy or {})
        WorkflowDefinitionService.require_admin(user, department)
        WorkflowDefinitionService.validate_assignment(user.company_id, reviewer)
        approver = approver or reviewer
        WorkflowDefinitionService.validate_assignment(user.company_id, approver)
        definition = WorkflowDefinitionService.create(
            user,
            name=name,
            code=code,
            workflow_type="contract_review",
            description="Source-grounded contract review, correction and approval.",
            department_id=department,
        )
        steps, transitions = graph(policy, reviewer, approver)
        version = WorkflowDefinitionService.publish(
            user,
            definition,
            entry_step=OPERATIONS[0],
            steps=steps,
            transitions=transitions,
            allowed_document_types=[],
            configuration={"max_attempts": 4},
        )
        return definition, version

    @staticmethod
    def validate_version(version, options):
        policy = ContractPolicy.validate(options)
        actual = {s.key: s for s in version.steps.all()}
        review = actual.get("review_if_required")
        approval = actual.get("approve_if_required")
        if not review or not approval:
            raise ValidationError(
                "Contract workflow requires review and approval boundaries."
            )
        expected, edges = graph(
            policy,
            review.configuration.get("assignment"),
            approval.configuration.get("assignment"),
        )
        if version.entry_step != OPERATIONS[0] or set(actual) != {
            s["key"] for s in expected
        }:
            raise ValidationError("Contract workflow requires the complete lifecycle.")
        for spec in expected:
            step = actual[spec["key"]]
            if step.step_type != spec["step_type"] or step.configuration != spec.get(
                "configuration", {}
            ):
                raise ValidationError("Contract lifecycle policy cannot be bypassed.")
        if set(
            version.transitions.values_list("source__key", "target__key", "outcome")
        ) != {(e["source"], e["target"], e["outcome"]) for e in edges}:
            raise ValidationError(
                "Contract workflow transitions bypass lifecycle boundaries."
            )
