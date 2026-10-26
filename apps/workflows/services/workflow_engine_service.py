import hashlib
import json
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService
from apps.workflows.models import (
    WorkflowAssignment,
    WorkflowCommand,
    WorkflowComment,
    WorkflowDefinition,
    WorkflowException,
    WorkflowFinding,
    WorkflowRun,
    WorkflowRunDocument,
    WorkflowStepRun,
    WorkflowVersion,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
    bounded_json,
)
from apps.workflows.services.workflow_events import notify_workflow, workflow_event


class WorkflowConflict(APIException):
    status_code = 409
    default_detail = "Workflow revision or idempotency conflict. Refresh the run."


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False, default=str).encode()
    ).hexdigest()


class WorkflowEngineService:
    CLOSED = {"approved", "rejected", "completed", "failed", "cancelled"}

    @staticmethod
    def locked(run):
        # Do not select nullable relations in a FOR UPDATE query (PostgreSQL).
        return WorkflowRun.objects.select_for_update().get(pk=run.pk, is_deleted=False)

    @staticmethod
    def enqueue(run):
        from apps.workflows.tasks.workflow_tasks import execute_workflow

        run_uuid, company_id, revision = str(run.uuid), run.company_id, run.revision
        transaction.on_commit(
            lambda: execute_workflow.delay(company_id, run_uuid, revision), robust=True
        )

    @classmethod
    @transaction.atomic
    def start(
        cls, user, *, version_uuid, document_uuids, idempotency_key, metadata=None
    ):
        if not WorkflowPermissionService.active(user):
            raise PermissionDenied("An active company account is required.")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ValidationError("A bounded idempotency key is required.")
        metadata = metadata or {}
        if not isinstance(metadata, dict):
            raise ValidationError("Workflow metadata must be an object.")
        bounded_json(metadata, 8192)
        if (
            not isinstance(document_uuids, list)
            or not 1 <= len(document_uuids) <= 20
            or len(set(map(str, document_uuids))) != len(document_uuids)
        ):
            raise ValidationError("Select 1–20 distinct documents.")
        version = (
            WorkflowVersion.objects.select_related("definition")
            .filter(
                uuid=version_uuid,
                definition__company_id=user.company_id,
                is_deleted=False,
            )
            .first()
        )
        if not version:
            raise PermissionDenied("Workflow version is unavailable.")
        # Serialize starts on the definition, including duplicate idempotency keys.
        definition = WorkflowDefinition.objects.select_for_update().get(
            pk=version.definition_id
        )
        if (
            not definition.is_active
            or definition.is_deleted
            or not WorkflowPermissionService.department_allowed(
                user, definition.department_id
            )
        ):
            raise PermissionDenied("Workflow definition is not available to start.")
        docs = list(
            Document.objects.filter(
                uuid__in=document_uuids,
                company_id=user.company_id,
                scope="internal",
                is_deleted=False,
            ).order_by("pk")
        )
        if len(docs) != len(document_uuids) or any(
            not DocumentPermissionService.can_action(user, d, "edit") for d in docs
        ):
            raise PermissionDenied(
                "Current permission is required for every related document."
            )
        if version.allowed_document_types and any(
            d.document_type not in version.allowed_document_types for d in docs
        ):
            raise ValidationError("Document type is not supported by this version.")
        if definition.workflow_type == "invoice":
            # Direct workflow starts must not bypass invoice batch preflight.
            from apps.documents.services.invoice_readiness_service import (
                InvoiceReadinessService,
            )

            for document in docs:
                InvoiceReadinessService.require_batch_source(user, document)
        for step in version.steps.all():
            WorkflowDefinitionService.validate_config(
                user.company_id, step.step_type, step.configuration
            )
            action = {"ai": "ai", "extraction": "extract", "approval": "correct"}.get(
                step.step_type
            )
            if action and any(
                not DocumentPermissionService.can_action(user, d, action) for d in docs
            ):
                raise PermissionDenied("Document policies do not permit this workflow.")
        from apps.workflows.services.workflow_processing_service import (
            WorkflowProcessingService,
        )

        WorkflowProcessingService.validate_start(user, version, docs)
        request_hash = fingerprint(
            {
                "user": user.pk,
                "version": str(version.uuid),
                "documents": sorted(map(str, document_uuids)),
                "metadata": metadata,
            }
        )
        existing = WorkflowRun.objects.filter(
            company_id=user.company_id, idempotency_key=idempotency_key
        ).first()
        if existing:
            if existing.request_fingerprint != request_hash:
                raise WorkflowConflict()
            WorkflowPermissionService.require(user, existing, "view")
            return existing, False
        # Company lock also serializes a key reused across different definitions.
        from apps.tenants.models import Company

        Company.objects.select_for_update().get(pk=user.company_id)
        existing = WorkflowRun.objects.filter(
            company_id=user.company_id, idempotency_key=idempotency_key
        ).first()
        if existing:
            if existing.request_fingerprint != request_hash:
                raise WorkflowConflict()
            WorkflowPermissionService.require(user, existing, "view")
            return existing, False
        run = WorkflowRun.objects.create(
            company=user.company,
            version=version,
            department=definition.department,
            initiated_by=user,
            status="queued",
            current_step=version.steps.get(key=version.entry_step),
            metadata=metadata,
            started_at=timezone.now(),
            idempotency_key=idempotency_key,
            request_fingerprint=request_hash,
            correlation_id=uuid.uuid4(),
        )
        for doc in docs:
            WorkflowRunDocument.objects.create(
                run=run, document=doc, governance_revision=doc.governance_revision
            )
        WorkflowStepRun.objects.bulk_create(
            [WorkflowStepRun(run=run, step=step) for step in version.steps.all()]
        )
        workflow_event(run, user, "started", version=version.number)
        cls.enqueue(run)
        return run, True

    @classmethod
    def set_state(cls, run, status, actor=None, event=None):
        run.status = status
        run.revision += 1
        if status in cls.CLOSED:
            run.completed_at = timezone.now()
            run.step_runs.filter(status__in=["pending", "waiting", "running"]).update(
                status="skipped"
                if status in {"completed", "approved", "rejected"}
                else "cancelled",
                completed_at=run.completed_at,
            )
        run.save(
            update_fields=[
                "status",
                "revision",
                "current_step",
                "completed_at",
                "error_type",
                "error_message",
                "updated_at",
            ]
        )
        if event:
            workflow_event(run, actor, event, revision=run.revision, state=status)

    @classmethod
    def advance(cls, run, step_run, outcome, actor=None):
        edge = step_run.step.outgoing.filter(
            outcome=outcome, version=run.version
        ).first()
        if not edge:
            raise ValidationError(
                "This outcome is not configured for the workflow step."
            )
        step_run.status = "completed"
        step_run.completed_at = timezone.now()
        step_run.save(update_fields=["status", "completed_at", "updated_at"])
        workflow_event(
            run, actor, "step_completed", step=step_run.step.key, outcome=outcome
        )
        run.current_step = edge.target
        cls.set_state(run, "queued", actor)
        cls.enqueue(run)

    @classmethod
    def assign(cls, run, step_run, actor, principal):
        WorkflowDefinitionService.validate_assignment(run.company_id, principal)
        candidate = (
            User.objects.get(pk=principal["user"]) if principal.get("user") else None
        )
        if candidate and (
            not WorkflowPermissionService.content_allowed(candidate, run)
            or any(
                not DocumentPermissionService.can_action(candidate, d, "correct")
                for d in run.documents.all()
            )
        ):
            raise ValidationError(
                "Assigned user must have current workflow and document review access."
            )
        assignment, _ = WorkflowAssignment.objects.update_or_create(
            step_run=step_run,
            defaults={
                "user": candidate,
                "department_id": principal.get("department"),
                "role": principal.get("role", ""),
                "assigned_by": actor,
            },
        )
        if step_run.approval_id:
            step_run.approval.assigned_to = candidate
            step_run.approval.save(update_fields=["assigned_to", "updated_at"])
        workflow_event(
            run,
            actor,
            "assignment_changed",
            step=step_run.step.key,
            user=assignment.user_id,
            department=assignment.department_id,
            role=assignment.role,
        )
        recipients = (
            [candidate]
            if candidate
            else list(
                User.objects.filter(company=run.company, is_active=True).filter(
                    **({"role": assignment.role} if assignment.role else {})
                )
            )
        )
        recipients = [
            u for u in recipients if WorkflowPermissionService.assigned(u, step_run)
        ]
        notify_workflow(run, actor, "assigned", recipients)
        run.exceptions.filter(step_run=step_run, status="open").update(owner=candidate)
        return assignment

    @classmethod
    def raise_exception(cls, run, step_run, category, actor=None):
        if not step_run.step.outgoing.filter(outcome="exception").exists():
            raise ValidationError(
                "This workflow step does not support exception resolution."
            )
        exception = WorkflowException.objects.create(
            run=run,
            step_run=step_run,
            category=category,
            safe_message="Workflow requires authorized exception review.",
            owner=actor,
        )
        step_run.status = "waiting"
        step_run.save(update_fields=["status", "updated_at"])
        cls.set_state(run, "exception", actor, "exception_created")
        notify_workflow(run, actor, "exception_raised")
        return exception

    @classmethod
    @transaction.atomic
    def command(
        cls, user, run, *, action, expected_revision, idempotency_key, **payload
    ):
        run = cls.locked(run)
        step_run = (
            run.step_runs.select_related("step").filter(step=run.current_step).first()
        )
        required_action = (
            "review" if action in {"accept", "request_changes"} else action
        )
        WorkflowPermissionService.require(user, run, "view")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ValidationError("An idempotency key is required.")
        bounded_json(payload)
        request_hash = fingerprint(
            {
                "actor": user.pk,
                "action": action,
                "revision": expected_revision,
                "payload": payload,
            }
        )
        previous = WorkflowCommand.objects.filter(run=run, key=idempotency_key).first()
        if previous:
            if previous.fingerprint != request_hash:
                raise WorkflowConflict()
            return run
        WorkflowPermissionService.require(user, run, required_action, step_run)
        if run.revision != expected_revision:
            raise WorkflowConflict()
        if run.status in cls.CLOSED:
            raise ValidationError("Closed workflows cannot be transitioned.")
        from apps.workflows.services.workflow_processing_service import (
            WorkflowProcessingService,
        )

        WorkflowProcessingService.validate_command(user, run, action)
        if action == "cancel":
            from apps.approvals.models import ApprovalRequest

            approval_ids = list(
                run.step_runs.exclude(approval=None).values_list(
                    "approval_id", flat=True
                )
            )
            list(
                ApprovalRequest.objects.select_for_update().filter(pk__in=approval_ids)
            )
            ApprovalRequest.objects.filter(
                pk__in=approval_ids,
                status__in=["pending", "in_review", "changes_requested"],
            ).update(status="cancelled")
            run.exceptions.filter(status="open").update(
                status="resolved",
                resolved_by=user,
                resolved_at=timezone.now(),
                resolution="Workflow cancelled.",
            )
            cls.set_state(run, "cancelled", user, "cancelled")
            notify_workflow(run, user, "cancelled")
        elif action == "assign":
            if not step_run or step_run.status != "waiting":
                raise ValidationError("Assignments may change only for a waiting step.")
            cls.assign(run, step_run, user, payload.get("assignment", {}))
            cls.set_state(run, run.status, user)
        elif (
            action in {"approve", "reject", "request_changes"}
            and run.status == "approval_required"
        ):
            from apps.approvals.services import ApprovalWorkflowService

            decision = getattr(ApprovalWorkflowService, action)
            decision(
                user=user,
                approval_request=step_run.approval,
                reviewer_notes=payload.get("comment") or "Workflow decision.",
            )
            run.refresh_from_db()
        elif (
            action in {"accept", "reject"}
            and run.status == "review_required"
            and step_run.step.step_type == "human_review"
        ):
            cls.advance(
                run, step_run, "accepted" if action == "accept" else "rejected", user
            )
            workflow_event(
                run, user, "review_submitted", step=step_run.step.key, outcome=action
            )
        elif (
            action == "request_changes"
            and run.status == "review_required"
            and step_run.step.step_type == "human_review"
        ):
            cls.raise_exception(run, step_run, "review_exception", user)
        elif action == "resolve_exception" and run.status == "exception":
            # Invoice exception recovery is valid only at the step that is
            # currently waiting for an authorized reviewer.
            if run.version.definition.workflow_type == "invoice" and (
                step_run is None or step_run.status != "waiting"
            ):
                raise ValidationError(
                    "Invoice exception recovery requires the active waiting step."
                )
            # Soft-deleted exceptions are not visible through the API and must
            # never be usable as a transition token.
            exception = run.exceptions.filter(
                uuid=payload.get("exception"),
                status="open",
                step_run=step_run,
                is_deleted=False,
            ).first()
            if not exception or not payload.get("resolution"):
                raise ValidationError("An open exception and resolution are required.")
            outcome = payload.get("outcome", "exception")
            if outcome != "exception":
                raise ValidationError(
                    "Exception resolution follows the configured exception transition."
                )
            cls.advance(run, step_run, outcome, user)
            (
                exception.status,
                exception.resolved_by,
                exception.resolved_at,
                exception.resolution,
            ) = "resolved", user, timezone.now(), payload["resolution"]
            exception.save(
                update_fields=[
                    "status",
                    "resolved_by",
                    "resolved_at",
                    "resolution",
                    "updated_at",
                ]
            )
            workflow_event(
                run, user, "exception_resolved", exception=str(exception.uuid)
            )
        elif action == "comment":
            if (
                not isinstance(payload.get("comment"), str)
                or not 1 <= len(payload["comment"]) <= 4000
            ):
                raise ValidationError("A bounded comment is required.")
            WorkflowComment.objects.create(
                run=run, step_run=step_run, author=user, body=payload["comment"]
            )
            cls.set_state(run, run.status, user)
            workflow_event(run, user, "comment_added")
            if run.status == "queued":
                cls.enqueue(run)
        else:
            raise ValidationError(
                "The command is not allowed in the current workflow state."
            )
        if action != "comment" and payload.get("comment"):
            WorkflowComment.objects.create(
                run=run, step_run=step_run, author=user, body=payload["comment"]
            )
        WorkflowCommand.objects.create(
            run=run,
            actor=user,
            key=idempotency_key,
            fingerprint=request_hash,
            action=action,
            resulting_revision=run.revision,
        )
        return run

    @classmethod
    @transaction.atomic
    def approval_decided(cls, approval):
        step_run = WorkflowStepRun.objects.filter(approval=approval).first()
        if not step_run:
            return
        run = cls.locked(step_run.run)
        if (
            run.status != "approval_required"
            or run.current_step_id != step_run.step_id
            or step_run.status != "waiting"
        ):
            raise ValidationError("Workflow approval is no longer awaiting a decision.")
        outcome = {
            "approved": "approved",
            "rejected": "rejected",
            "changes_requested": "exception",
        }.get(approval.status)
        if outcome == "exception":
            cls.raise_exception(
                run, step_run, "changes_requested", approval.reviewed_by
            )
        elif outcome:
            from apps.workflows.services.workflow_processing_service import (
                WorkflowProcessingService,
            )

            WorkflowProcessingService.validate_command(
                approval.reviewed_by, run, outcome
            )
            cls.advance(run, step_run, outcome, approval.reviewed_by)
            workflow_event(
                run,
                approval.reviewed_by,
                "approval_granted" if outcome == "approved" else "approval_rejected",
                step=step_run.step.key,
            )

    @classmethod
    @transaction.atomic
    def review_finding(cls, user, run, finding_uuid, *, status, owner_id=None):
        run = cls.locked(run)
        if run.version.definition.workflow_type == "compliance":
            from apps.workflows.models.compliance import ComplianceAssessment
            from apps.workflows.services.compliance_assessment_service import (
                ComplianceAssessmentService,
            )

            assessment = ComplianceAssessment.objects.filter(
                run=run, company_id=run.company_id, is_deleted=False
            ).first()
            if not assessment:
                raise PermissionDenied("Compliance assessment is unavailable.")
            ComplianceAssessmentService.authorize(user, assessment)
            ComplianceAssessmentService.editor(user, assessment)
        if run.version.definition.workflow_type == "finance_audit":
            from apps.workflows.models.finance import FinanceAudit
            from apps.workflows.services.finance_audit_service import (
                FinanceAuditService,
            )

            audit = FinanceAudit.objects.filter(
                company_id=run.company_id, run=run, is_deleted=False
            ).first()
            if not audit:
                raise PermissionDenied("Finance audit is unavailable.")
            FinanceAuditService.authorize(user, audit)
            FinanceAuditService.editor(user, audit)
        finding = WorkflowFinding.objects.filter(
            run=run, uuid=finding_uuid, is_deleted=False
        ).first()
        if not finding:
            raise ValidationError("Finding is unavailable.")
        WorkflowPermissionService.require(user, run, "review", finding.step_run)
        # Invoice findings are decisions for the active human-review boundary.
        # A completed or moved-on invoice must retain its recorded decisions.
        if run.version.definition.workflow_type == "invoice":
            active = (
                run.step_runs.select_related("step")
                .filter(step_id=run.current_step_id)
                .first()
            )
            if (
                run.status != "review_required"
                or active is None
                or active.status != "waiting"
                or active.step.step_type != "human_review"
                or finding.step_run_id != active.pk
            ):
                raise ValidationError(
                    "Invoice findings may be reviewed only at the active review step."
                )
        if status not in {"accepted", "dismissed", "resolved"}:
            raise ValidationError("Invalid finding review state.")
        owner = None
        if owner_id:
            owner = User.objects.filter(
                pk=owner_id, company_id=run.company_id, is_active=True
            ).first()
            if not owner or not WorkflowPermissionService.content_allowed(owner, run):
                raise ValidationError("Finding owner must have company-scoped access.")
        finding.status, finding.review_state, finding.owner = status, "reviewed", owner
        finding.save(update_fields=["status", "review_state", "owner", "updated_at"])
        workflow_event(
            run, user, "finding_reviewed", finding=str(finding.uuid), status=status
        )
        return finding
