from uuid import UUID, uuid4
import re

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.workflows.models import HRPolicy
from apps.workflows.services.hr_policy_service import (
    HRPolicyService,
    digest,
    categories,
)
from apps.workflows.services.contract_extraction_service import evidence
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowEngineService,
    WorkflowConflict,
)
from apps.workflows.services.workflow_processing_service import ProcessingResult


def options(value):
    if not isinstance(value, dict) or set(value) != {"policy_uuid"}:
        raise ValidationError("A policy UUID is required.")
    try:
        return {"policy_uuid": str(UUID(str(value["policy_uuid"])))}
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Invalid policy UUID.")


def graph(policy, reviewer, approver):
    def processor(key):
        return {
            "key": key,
            "name": key.replace("_", " ").title(),
            "step_type": "processing",
            "configuration": {
                "adapter": "hr_policy",
                "operation": key,
                "options": policy,
                "exception_assignment": reviewer,
            },
        }

    steps = [
        processor("ingest_policy"),
        {
            "key": "review_policy",
            "name": "Review policy metadata and coverage",
            "step_type": "human_review",
            "configuration": {"assignment": reviewer},
        },
        processor("prepare_policy"),
        {
            "key": "approve_policy",
            "name": "Approve policy publication",
            "step_type": "approval",
            "configuration": {"assignment": approver},
        },
        processor("publish_policy"),
        {"key": "completed", "name": "Completed", "step_type": "terminal"},
        {
            "key": "rejected",
            "name": "Rejected",
            "step_type": "terminal",
            "configuration": {"status": "rejected"},
        },
    ]
    edges = [
        ("ingest_policy", "review_policy", "success"),
        ("ingest_policy", "review_policy", "exception"),
        ("review_policy", "prepare_policy", "accepted"),
        ("review_policy", "prepare_policy", "exception"),
        ("review_policy", "rejected", "rejected"),
        ("prepare_policy", "approve_policy", "success"),
        ("approve_policy", "publish_policy", "approved"),
        ("approve_policy", "rejected", "rejected"),
        ("approve_policy", "rejected", "exception"),
        ("publish_policy", "completed", "success"),
    ]
    return steps, [{"source": a, "target": b, "outcome": c} for a, b, c in edges]


class HRPolicyWorkflowService:
    @staticmethod
    @transaction.atomic
    def submit(user, policy, *, revision, reviewer, approver, idempotency_key):
        HRPolicyService.fresh(user)
        policy = HRPolicyService.locked(policy)
        if not HRPolicyService.visible(user).filter(pk=policy.pk).exists():
            raise PermissionDenied("Policy is unavailable.")
        WorkflowDefinitionService.require_admin(user, policy.department_id)
        if policy.revision != revision:
            raise WorkflowConflict()
        if policy.review_run_id:
            run = WorkflowEngineService.locked(policy.review_run)
            if run.idempotency_key == idempotency_key:
                if (
                    run.metadata.get("policy_revision") != revision
                    or run.metadata.get("reviewer") != reviewer
                    or run.metadata.get("approver") != approver
                ):
                    raise WorkflowConflict()
                return run
            if run.status not in WorkflowEngineService.CLOSED:
                raise WorkflowConflict("Policy already has an active review.")
        WorkflowDefinitionService.validate_assignment(user.company_id, reviewer)
        WorkflowDefinitionService.validate_assignment(user.company_id, approver)
        doc = HRPolicyService.source(user, policy)[0]
        definition = WorkflowDefinitionService.create(
            user,
            name=policy.title[:150],
            code="hr-" + policy.uuid.hex + "-" + uuid4().hex[:8],
            workflow_type="hr_policy",
            department_id=policy.department_id,
        )
        steps, transitions = graph(
            {"policy_uuid": str(policy.uuid)}, reviewer, approver
        )
        version = WorkflowDefinitionService.publish(
            user,
            definition,
            entry_step="ingest_policy",
            steps=steps,
            transitions=transitions,
            allowed_document_types=[],
            configuration={"max_attempts": 4},
        )
        run = WorkflowEngineService.start(
            user,
            version_uuid=version.uuid,
            document_uuids=[str(doc.uuid)],
            idempotency_key=idempotency_key,
            metadata={
                "hr_policy_uuid": str(policy.uuid),
                "policy_revision": revision,
                "reviewer": reviewer,
                "approver": approver,
            },
        )[0]
        policy.review_run = run
        policy.save(update_fields=["review_run", "updated_at"])
        return run


class HRPolicyProcessingAdapter:
    @staticmethod
    def snapshot(operation, policy):
        return {"schema_version": 1}

    @staticmethod
    def validate_config(company_id, operation, value):
        value = options(value)
        if (
            operation not in {"ingest_policy", "prepare_policy", "publish_policy"}
            or not HRPolicy.objects.filter(
                uuid=value["policy_uuid"], company_id=company_id, is_deleted=False
            ).exists()
        ):
            raise ValidationError("Policy processing configuration is unavailable.")

    @staticmethod
    def validate_version(version, value):
        actual = {s.key: s for s in version.steps.all()}
        if "review_policy" not in actual or "approve_policy" not in actual:
            raise ValidationError("Policy publication requires review and approval.")
        steps, transitions = graph(
            options(value),
            actual["review_policy"].configuration.get("assignment"),
            actual["approve_policy"].configuration.get("assignment"),
        )
        if (
            version.entry_step != "ingest_policy"
            or set(actual) != {s["key"] for s in steps}
            or any(
                actual[s["key"]].step_type != s["step_type"]
                or actual[s["key"]].configuration != s.get("configuration", {})
                for s in steps
            )
        ):
            raise ValidationError("Policy lifecycle cannot be bypassed.")
        if set(
            version.transitions.values_list("source__key", "target__key", "outcome")
        ) != {(e["source"], e["target"], e["outcome"]) for e in transitions}:
            raise ValidationError("Policy lifecycle transitions cannot be bypassed.")

    @staticmethod
    def validate_start(user, documents, value):
        policy = (
            HRPolicyService.visible(user)
            .filter(uuid=options(value)["policy_uuid"])
            .first()
        )
        if not policy:
            raise PermissionDenied("Policy is unavailable.")
        WorkflowDefinitionService.require_admin(user, policy.department_id)
        if (
            len(documents) != 1
            or documents[0].pk != HRPolicyService.source(user, policy)[0].pk
        ):
            raise ValidationError("Policy requires its pinned source document.")

    @staticmethod
    def validate_command(user, run, action, value):
        policy = (
            HRPolicyService.visible(user)
            .filter(uuid=options(value)["policy_uuid"])
            .first()
        )
        if (
            not policy
            or policy.review_run_id != run.pk
            or policy.revision != run.metadata.get("policy_revision")
        ):
            raise WorkflowConflict("Policy review is stale.")
        if action in {"accept", "approve", "approved", "resolve_exception"}:
            doc = HRPolicyService.source(user, policy, complete=True)[0]
            if not run.documents.filter(pk=doc.pk).exists():
                raise WorkflowConflict(
                    "Source replacement requires a new policy review."
                )

    @staticmethod
    def execute(run, step_run, operation, value):
        policy = HRPolicy.objects.get(
            uuid=options(value)["policy_uuid"],
            company_id=run.company_id,
            is_deleted=False,
        )
        if policy.review_run_id != run.pk or policy.revision != run.metadata.get(
            "policy_revision"
        ):
            raise WorkflowConflict("Policy review is stale.")
        doc, text, coverage, draft = HRPolicyService.source(
            run.initiated_by, policy, complete=operation != "ingest_policy"
        )
        if not run.documents.filter(pk=doc.pk).exists():
            raise WorkflowConflict("Source replacement requires a new policy review.")
        if operation == "ingest_policy":
            proposals, proofs, uncertain = {}, [], False
            fields = {
                "title": "policy title",
                "category": "policy category",
                "version": "policy version",
                "effective_from": "effective from",
                "effective_until": "effective until",
                "department": "department",
                "employee_categories": "employee categories",
                "locations": "locations",
                "supersedes": "supersedes",
            }
            if coverage["complete"]:
                for key, label in fields.items():
                    matches = list(
                        re.finditer(
                            r"(?im)^" + re.escape(label) + r"\s*:\s*([^\r\n]{1,200})$",
                            text,
                        )
                    )
                    if len(matches) != 1:
                        uncertain = uncertain or len(matches) > 1
                        continue
                    raw = matches[0][1].strip()
                    proposals[key] = {
                        "value": raw,
                        "trusted": False,
                        "review_required": True,
                    }
                    proofs.append(
                        evidence(doc, text, matches[0].start(1), matches[0].end(1))
                    )
                    if key in {"effective_from", "effective_until"}:
                        from datetime import date

                        try:
                            date.fromisoformat(raw)
                        except ValueError:
                            uncertain = True
                    if (
                        key == "category"
                        and not categories(run.initiated_by).filter(code=raw).exists()
                    ):
                        uncertain = True
            needs_review = (
                uncertain or not coverage["complete"] or not draft.get("effective_from")
            )
            return ProcessingResult(
                {
                    "coverage": coverage,
                    "metadata": draft,
                    "extracted_metadata": proposals,
                    "review_required": needs_review,
                },
                evidence=proofs,
                exception_category="policy_metadata_review" if needs_review else None,
            )
        if operation == "prepare_policy":
            return ProcessingResult(
                {
                    "source_digest": digest(text),
                    "governance_revision": doc.governance_revision,
                    "policy_revision": policy.revision,
                }
            )
        version = HRPolicyService.publish(run)
        return ProcessingResult(
            {"policy_version_uuid": str(version.uuid), "number": version.number}
        )
