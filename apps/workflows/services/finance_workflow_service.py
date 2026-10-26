from uuid import UUID, uuid4

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.workflows.models import FinanceAudit
from apps.workflows.services.finance_audit_service import FinanceAuditService
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowEngineService,
    WorkflowConflict,
)
from apps.workflows.services.workflow_processing_service import ProcessingResult


def source_ids(audit):
    return set(audit.datasets.values_list("document_id", flat=True)) | set(
        audit.evidence_links.values_list("document_id", flat=True)
    )


def documents(audit):
    from apps.documents.models import Document

    return Document.objects.filter(pk__in=source_ids(audit)).order_by("pk")


def options(value):
    if not isinstance(value, dict) or set(value) != {"audit_uuid"}:
        raise ValidationError("A finance audit UUID is required.")
    try:
        return {"audit_uuid": str(UUID(str(value["audit_uuid"])))}
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Invalid Finance audit project UUID.")


def graph(project, reviewer, approver):
    def processor(key):
        return {
            "key": key,
            "name": key.replace("_", " ").title(),
            "step_type": "processing",
            "configuration": {
                "adapter": "finance_audit",
                "operation": key,
                "options": project,
                "exception_assignment": reviewer,
            },
        }

    steps = [
        processor("validate_scope"),
        processor("perform_audit"),
        {
            "key": "review_audit",
            "name": "Review finance tests, findings and evidence",
            "step_type": "human_review",
            "configuration": {"assignment": reviewer},
        },
        processor("validate_audit"),
        {
            "key": "approve_audit",
            "name": "Approve finance audit output",
            "step_type": "approval",
            "configuration": {"assignment": approver},
        },
        processor("finalize_audit"),
        {"key": "completed", "name": "Completed", "step_type": "terminal"},
        {
            "key": "rejected",
            "name": "Rejected",
            "step_type": "terminal",
            "configuration": {"status": "rejected"},
        },
    ]
    edges = [
        ("validate_scope", "perform_audit", "success"),
        ("validate_scope", "review_audit", "exception"),
        ("perform_audit", "review_audit", "success"),
        ("perform_audit", "review_audit", "exception"),
        ("review_audit", "validate_audit", "accepted"),
        ("review_audit", "rejected", "rejected"),
        ("review_audit", "rejected", "exception"),
        ("validate_audit", "approve_audit", "success"),
        ("validate_audit", "rejected", "exception"),
        ("approve_audit", "finalize_audit", "approved"),
        ("approve_audit", "rejected", "rejected"),
        ("approve_audit", "rejected", "exception"),
        ("finalize_audit", "completed", "success"),
        ("finalize_audit", "rejected", "exception"),
    ]
    return steps, [{"source": a, "target": b, "outcome": c} for a, b, c in edges]


class FinanceWorkflowService:
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
        project = FinanceAuditService.locked(user, project)
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
                raise WorkflowConflict(
                    "Finance audit project already has an active workflow."
                )
        if project.revision != revision:
            raise WorkflowConflict()
        FinanceAuditService.authorize(user, project)
        WorkflowDefinitionService.validate_assignment(user.company_id, reviewer)
        WorkflowDefinitionService.validate_assignment(user.company_id, approver)
        definition = WorkflowDefinitionService.create(
            user,
            name=project.title[:150],
            code="finance-" + project.uuid.hex + "-" + uuid4().hex[:8],
            workflow_type="finance_audit",
            department_id=project.department_id,
        )
        steps, transitions = graph(
            {"audit_uuid": str(project.uuid)}, reviewer, approver
        )
        version = WorkflowDefinitionService.publish(
            user,
            definition,
            entry_step="validate_scope",
            steps=steps,
            transitions=transitions,
            allowed_document_types=[],
            configuration={"max_attempts": 4},
        )
        run = WorkflowEngineService.start(
            user,
            version_uuid=version.uuid,
            document_uuids=[str(doc.uuid) for doc in documents(project)],
            idempotency_key=idempotency_key,
            metadata={
                "finance_audit_uuid": str(project.uuid),
                "audit_revision": revision,
                "start_revision": revision,
                "reviewer": reviewer,
                "approver": approver,
            },
        )[0]
        project.run = run
        project.save(update_fields=["run", "updated_at"])
        return run


class FinanceProcessingAdapter:
    @staticmethod
    def snapshot(operation, value):
        return {"schema_version": 1}

    @staticmethod
    def validate_config(company_id, operation, value):
        if (
            operation
            not in {
                "validate_scope",
                "perform_audit",
                "validate_audit",
                "finalize_audit",
            }
            or not FinanceAudit.objects.filter(
                uuid=options(value)["audit_uuid"],
                company_id=company_id,
                is_deleted=False,
            ).exists()
        ):
            raise ValidationError(
                "Finance audit processing configuration is unavailable."
            )

    @staticmethod
    def validate_version(version, value):
        actual = {s.key: s for s in version.steps.all()}
        if "review_audit" not in actual or "approve_audit" not in actual:
            raise ValidationError("Finance audits require review and approval.")
        steps, transitions = graph(
            options(value),
            actual["review_audit"].configuration.get("assignment"),
            actual["approve_audit"].configuration.get("assignment"),
        )
        if (
            version.entry_step != "validate_scope"
            or set(actual) != {s["key"] for s in steps}
            or any(
                actual[s["key"]].step_type != s["step_type"]
                or actual[s["key"]].configuration != s.get("configuration", {})
                for s in steps
            )
        ):
            raise ValidationError("Finance audit lifecycle cannot be bypassed.")
        if set(
            version.transitions.values_list("source__key", "target__key", "outcome")
        ) != {(e["source"], e["target"], e["outcome"]) for e in transitions}:
            raise ValidationError(
                "Finance audit lifecycle transitions cannot be bypassed."
            )

    @staticmethod
    def validate_start(user, documents, value):
        project = (
            FinanceAuditService.visible(user)
            .filter(uuid=options(value)["audit_uuid"])
            .first()
        )
        if not project:
            raise PermissionDenied("Finance audit project is unavailable.")
        WorkflowDefinitionService.require_admin(user, project.department_id)
        FinanceAuditService.authorize(user, project)
        if (
            project.run_id and project.run.status not in WorkflowEngineService.CLOSED
        ) or {d.pk for d in documents} != set(source_ids(project)):
            raise ValidationError(
                "Finance audit workflow requires its exact pinned financial source package."
            )

    @staticmethod
    def project(run, value):
        project = FinanceAudit.objects.get(
            uuid=options(value)["audit_uuid"],
            company_id=run.company_id,
            is_deleted=False,
        )
        if project.run_id != run.pk or project.revision != run.metadata.get(
            "audit_revision"
        ):
            raise WorkflowConflict("Finance audit workflow revision is stale.")
        if set(run.documents.values_list("pk", flat=True)) != set(source_ids(project)):
            raise WorkflowConflict("Financial source package changed.")
        return project

    @classmethod
    def validate_command(cls, user, run, action, value):
        project = cls.project(run, value)
        FinanceAuditService.authorize(user, project)
        if action in {"accept", "approve", "approved"}:
            FinanceAuditService.readiness(user, project)

    @classmethod
    def execute(cls, run, step_run, operation, value):
        project = cls.project(run, value)
        FinanceAuditService.authorize(run.initiated_by, project)
        if operation == "validate_scope":
            if not project.datasets.exists():
                raise ValidationError("An audit dataset is required.")
            return ProcessingResult(
                {"population_revision": project.population_revision}
            )
        if operation == "perform_audit":
            sample = None
            if project.policy.get("sampling"):
                sample = FinanceAuditService.sample(
                    run.initiated_by,
                    project,
                    revision=project.revision,
                    parameters=project.policy["sampling"],
                    idempotency_key="initial-sample-" + str(run.uuid),
                    internal=True,
                )
            result = FinanceAuditService.execute(
                run.initiated_by,
                project,
                revision=project.revision,
                sample_uuid=sample.uuid if sample else None,
                idempotency_key="initial-tests-" + str(run.uuid),
                internal=True,
            )
            run.refresh_from_db()
            return ProcessingResult(
                {"test_run_uuid": str(result.uuid), "counts": result.counts}
            )
        if operation == "validate_audit":
            FinanceAuditService.readiness(run.initiated_by, project)
            return ProcessingResult({"audit_revision": project.revision})
        output = FinanceAuditService.assemble(project)
        return ProcessingResult(
            {"output_uuid": str(output.uuid), "number": output.number}
        )
