from django.db import transaction

from apps.workflows.services.invoice_schema import InvoicePolicy
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)


class InvoiceDefinitionService:
    @staticmethod
    @transaction.atomic
    def create(
        user, *, name, code, reviewer, approver=None, department=None, policy=None
    ):
        policy = InvoicePolicy.validate(policy or {})
        WorkflowDefinitionService.require_admin(user, department)
        WorkflowDefinitionService.validate_assignment(user.company_id, reviewer)
        approver = approver or reviewer
        WorkflowDefinitionService.validate_assignment(user.company_id, approver)
        definition = WorkflowDefinitionService.create(
            user,
            name=name,
            code=code,
            workflow_type="invoice",
            description="Versioned invoice extraction, validation, review and approval.",
            department_id=department,
        )
        operations = [
            "validate_document",
            "classify_invoice",
            "extract_invoice_fields",
            "normalize_fields",
            "validate_fields",
            "validate_totals",
            "detect_duplicate",
            "match_vendor",
            "match_purchase_order",
            "assess_confidence",
        ]

        def processor(key):
            return {
                "key": key,
                "name": key.replace("_", " ").title(),
                "step_type": "processing",
                "configuration": {
                    "adapter": "invoice",
                    "operation": key,
                    "options": policy,
                    "exception_assignment": reviewer,
                },
            }

        steps = [processor(key) for key in operations]
        steps += [
            {
                "key": "review_decision",
                "name": "Review policy decision",
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
                "name": "Review invoice",
                "step_type": "human_review",
                "configuration": {"assignment": reviewer},
            },
            processor("prepare_reviewed_output"),
            {
                "key": "approval_decision",
                "name": "Approval policy decision",
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
                "name": "Approve invoice",
                "step_type": "approval",
                "configuration": {"assignment": approver},
            },
            processor("generate_normalized_invoice"),
            {"key": "completed", "name": "Completed", "step_type": "terminal"},
            {
                "key": "rejected",
                "name": "Rejected",
                "step_type": "terminal",
                "configuration": {"status": "rejected"},
            },
        ]
        transitions = [
            {"source": a, "target": b, "outcome": "success"}
            for a, b in zip(operations, operations[1:])
        ]
        transitions += [
            {
                "source": "extract_invoice_fields",
                "target": "normalize_fields",
                "outcome": "exception",
            },
            {
                "source": "assess_confidence",
                "target": "review_decision",
                "outcome": "success",
            },
            {
                "source": "review_decision",
                "target": "review_if_required",
                "outcome": "pass",
            },
            {
                "source": "review_decision",
                "target": "prepare_reviewed_output",
                "outcome": "fail",
            },
            {
                "source": "review_if_required",
                "target": "prepare_reviewed_output",
                "outcome": "accepted",
            },
            {
                "source": "review_if_required",
                "target": "rejected",
                "outcome": "rejected",
            },
            # Request changes pauses on the same review step; resolution then performs validation again.
            {
                "source": "review_if_required",
                "target": "prepare_reviewed_output",
                "outcome": "exception",
            },
            {
                "source": "prepare_reviewed_output",
                "target": "approval_decision",
                "outcome": "success",
            },
            {
                "source": "approval_decision",
                "target": "approve_if_required",
                "outcome": "pass",
            },
            {
                "source": "approval_decision",
                "target": "generate_normalized_invoice",
                "outcome": "fail",
            },
            {
                "source": "approve_if_required",
                "target": "generate_normalized_invoice",
                "outcome": "approved",
            },
            {
                "source": "approve_if_required",
                "target": "rejected",
                "outcome": "rejected",
            },
            {
                "source": "generate_normalized_invoice",
                "target": "completed",
                "outcome": "success",
            },
        ]
        version = WorkflowDefinitionService.publish(
            user,
            definition,
            entry_step=operations[0],
            steps=steps,
            transitions=transitions,
            allowed_document_types=[],
            configuration={"max_attempts": 4},
        )
        return definition, version
