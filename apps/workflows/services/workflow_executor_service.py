from django.db import transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.approvals.services import ApprovalWorkflowService
from apps.documents.services import DocumentPermissionService
from apps.workflows.models import (
    ExtractionRun,
    ExtractionTemplate,
    WorkflowArtifact,
    WorkflowFinding,
    WorkflowRun,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services.workflow_ai_service import WorkflowAIService
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
    bounded_json,
)
from apps.workflows.services.workflow_engine_service import WorkflowEngineService
from apps.workflows.services.workflow_events import notify_workflow, workflow_event


class WorkflowExecutorService:
    # Only server-installed adapters can execute. No URLs, eval or import paths
    # from workflow configuration are executed. Adapters must use the supplied
    # stable key for remote idempotency and enforce their own export policies.
    integration_adapters = {}

    @classmethod
    @transaction.atomic
    def execute(cls, company_id, run_uuid, expected_revision, attempt=1):
        run = (
            WorkflowRun.objects.select_for_update()
            .filter(company_id=company_id, uuid=run_uuid, is_deleted=False)
            .first()
        )
        if not run or run.status != "queued" or run.revision != expected_revision:
            return False
        step_run = run.step_runs.select_related("step").get(step=run.current_step)
        if step_run.status != "pending":
            return False
        user = run.initiated_by
        if not WorkflowPermissionService.content_allowed(user, run):
            cls.fail_locked(run, step_run, "permission_denied")
            return False
        try:
            WorkflowDefinitionService.validate_config(
                company_id, step_run.step.step_type, step_run.step.configuration
            )
            step_run.status, step_run.started_at = "running", timezone.now()
            step_run.attempt = attempt
            step_run.save(
                update_fields=["status", "started_at", "attempt", "updated_at"]
            )
            WorkflowEngineService.set_state(run, "processing", user, "step_started")
            with transaction.atomic():
                cls.execute_step(run, step_run)
        except (PermissionDenied, ValidationError, DjangoValidationError):
            # Permanent failures are final, never endlessly retried. Messages do
            # not contain provider output or confidential source content.
            run.refresh_from_db()
            step_run.refresh_from_db()
            cls.fail_locked(run, step_run, "policy_or_validation_failure")
        return True

    @classmethod
    def execute_step(cls, run, step_run):
        step, user = step_run.step, run.initiated_by
        config, kind = step.configuration, step.step_type
        if kind == "terminal":
            step_run.status, step_run.completed_at = "completed", timezone.now()
            step_run.save(update_fields=["status", "completed_at", "updated_at"])
            workflow_event(run, user, "step_completed", step=step.key)
            WorkflowEngineService.set_state(
                run, config.get("status", "completed"), user, "completed"
            )
            notify_workflow(run, user, "completed")
            return
        if kind in {"human_review", "approval"}:
            WorkflowEngineService.assign(run, step_run, user, config["assignment"])
            step_run.status = "waiting"
            if kind == "approval":
                # The existing approval request remains the authoritative decision
                # object. Its generic payload contains only the workflow UUID.
                primary_doc = run.documents.order_by("pk").first()
                step_run.approval = ApprovalWorkflowService.create_approval_request(
                    user=user,
                    document=primary_doc,
                    title="Workflow approval",
                    submitted_payload={"workflow_uuid": str(run.uuid)},
                    assigned_to=step_run.assignment.user,
                    review_type="workflow_review",
                )
            step_run.save(update_fields=["status", "approval", "updated_at"])
            WorkflowEngineService.set_state(
                run,
                "approval_required" if kind == "approval" else "review_required",
                user,
            )
            notify_workflow(
                run,
                user,
                "approval_required" if kind == "approval" else "review_required",
                [step_run.assignment.user] if step_run.assignment.user else None,
            )
            return
        if kind == "assignment":
            WorkflowEngineService.assign(run, step_run, user, config["assignment"])
        elif kind == "notification":
            notify_workflow(run, user, "notification")
        elif kind in {"rule", "validation"}:
            artifact = run.artifacts.filter(
                step_run__step__key=config["source_step"], is_deleted=False
            ).first()
            if not artifact:
                raise ValidationError("Rule input artifact is unavailable.")
            missing = object()
            value = artifact.data
            for key in config["path"].split("."):
                value = value.get(key, missing) if isinstance(value, dict) else missing
            op = config["operator"]
            expected = config.get("value")
            passed = value is not missing
            if op == "eq":
                passed = passed and value == expected
            elif op == "ne":
                passed = passed and value != expected
            elif op in {"gte", "lte"}:
                passed = (
                    passed
                    and type(value) in {int, float}
                    and (value >= expected if op == "gte" else value <= expected)
                )
            WorkflowEngineService.advance(
                run, step_run, "pass" if passed else "fail", user
            )
            return
        elif kind == "ai":
            output, evidence, execution = WorkflowAIService.execute(run, step_run)
            WorkflowArtifact.objects.create(
                run=run,
                step_run=step_run,
                kind="structured_ai",
                data=output,
                evidence=evidence,
            )
            for item in output["findings"]:
                WorkflowFinding.objects.create(
                    run=run,
                    step_run=step_run,
                    category=item["category"],
                    severity=item["severity"],
                    title=item["title"],
                    description=item["description"],
                    confidence=item["confidence"],
                    evidence=[
                        e for e in evidence if e["chunk_uuid"] in item["evidence"]
                    ],
                )
            step_run.execution_metadata = execution
            step_run.save(update_fields=["execution_metadata", "updated_at"])
        elif kind == "extraction":
            from apps.workflows.services import StructuredExtractionService

            template = ExtractionTemplate.objects.get(
                uuid=config["template"],
                company=run.company,
                is_active=True,
                is_deleted=False,
            )
            documents = list(run.documents.order_by("pk"))
            if len(documents) != 1:
                raise ValidationError(
                    "The existing extraction adapter requires one document."
                )
            DocumentPermissionService.require_action(user, documents[0], "extract")
            step_run.extraction = ExtractionRun.objects.create(
                company=run.company,
                document=documents[0],
                template=template,
                requested_by=user,
            )
            step_run.save(update_fields=["extraction", "updated_at"])
            extraction = StructuredExtractionService.run_extraction(
                step_run.extraction, field_specs=step.execution_snapshot["fields"]
            )
            if extraction.status == "failed":
                raise ValidationError("Extraction did not complete.")
            WorkflowArtifact.objects.create(
                run=run,
                step_run=step_run,
                kind="extracted_json",
                data=extraction.raw_output,
            )
            if extraction.status == "review_required":
                WorkflowEngineService.raise_exception(
                    run, step_run, "extraction_review_required", user
                )
                return
        elif kind == "processing":
            from apps.workflows.services.workflow_processing_service import (
                WorkflowProcessingService,
            )

            result = WorkflowProcessingService.adapter(config["adapter"]).execute(
                run, step_run, config["operation"], config.get("options", {})
            )
            bounded_json(result.data, 262144)
            WorkflowArtifact.objects.create(
                run=run,
                step_run=step_run,
                kind="processing_result",
                data=result.data,
                evidence=result.evidence,
            )
            if result.exception_category:
                if config.get("exception_assignment"):
                    WorkflowEngineService.assign(
                        run, step_run, user, config["exception_assignment"]
                    )
                WorkflowEngineService.raise_exception(
                    run, step_run, result.exception_category, user
                )
                return
        elif kind == "integration":
            # Any exported document content must have explicit export permission.
            if any(
                not DocumentPermissionService.can_action(user, d, "export")
                for d in run.documents.all()
            ):
                raise PermissionDenied(
                    "Document export policies deny workflow integration."
                )
            output = cls.integration_adapters[config["adapter"]](
                run=run,
                step_run=step_run,
                options=config.get("options", {}),
                idempotency_key=f"{run.uuid}:{step.uuid}",
            )
            if not isinstance(output, dict):
                raise ValidationError("Integration output must be structured JSON.")
            bounded_json(output)
            WorkflowArtifact.objects.create(
                run=run, step_run=step_run, kind="integration_result", data=output
            )
        WorkflowEngineService.advance(run, step_run, "success", user)

    @staticmethod
    def fail_locked(run, step_run, error_type):
        step_run.status, step_run.completed_at = "failed", timezone.now()
        step_run.error_type, step_run.error_message = (
            error_type,
            "Workflow step could not be completed safely.",
        )
        step_run.save(
            update_fields=[
                "status",
                "completed_at",
                "error_type",
                "error_message",
                "updated_at",
            ]
        )
        run.error_type, run.error_message = error_type, "Workflow execution failed."
        workflow_event(
            run,
            run.initiated_by,
            "step_failed",
            step=step_run.step.key,
            error_type=error_type,
        )
        WorkflowEngineService.set_state(run, "failed", run.initiated_by, "failed")
        notify_workflow(run, run.initiated_by, "failed")

    @classmethod
    @transaction.atomic
    def fail(cls, company_id, run_uuid, expected_revision, attempt=4):
        run = (
            WorkflowRun.objects.select_for_update()
            .filter(
                company_id=company_id,
                uuid=run_uuid,
                status="queued",
                revision=expected_revision,
            )
            .first()
        )
        if run:
            step_run = run.step_runs.get(step=run.current_step)
            step_run.attempt = attempt
            step_run.save(update_fields=["attempt", "updated_at"])
            cls.fail_locked(run, step_run, "execution_unavailable")
