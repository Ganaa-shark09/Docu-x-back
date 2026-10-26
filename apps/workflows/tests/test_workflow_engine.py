import json
import uuid
from unittest.mock import patch

from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import override_settings
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.approvals.models import ApprovalRequest
from apps.approvals.services import ApprovalWorkflowService
from apps.audit.models import AuditLog
from apps.documents.models import Document, DocumentAccessPolicy, DocumentFieldPolicy
from apps.notifications.models import Notification
from apps.tenants.models import Company
from apps.workspaces.models import Department, DepartmentMembership
from apps.workflows.models import WorkflowArtifact, WorkflowAssignment, WorkflowComment
from apps.workflows.models import WorkflowRun
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services.workflow_ai_service import WorkflowAIService
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
)
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
class WorkflowEngineTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Engine", slug="engine")
        cls.other_company = Company.objects.create(
            name="Foreign engine", slug="foreign-engine"
        )
        cls.admin = User.objects.create_user(
            username="engine-admin", company=cls.company, role="company_admin"
        )
        cls.reviewer = User.objects.create_user(
            username="engine-reviewer", company=cls.company, role="reviewer"
        )
        cls.member = User.objects.create_user(
            username="engine-member", company=cls.company
        )
        cls.foreign = User.objects.create_user(
            username="engine-foreign", company=cls.other_company, role="company_admin"
        )
        cls.department = Department.objects.create(company=cls.company, name="Legal")
        cls.private_department = Department.objects.create(
            company=cls.company, name="Private"
        )
        DepartmentMembership.objects.create(
            company=cls.company,
            department=cls.department,
            user=cls.reviewer,
            role="reviewer",
        )
        DepartmentMembership.objects.create(
            company=cls.company,
            department=cls.department,
            user=cls.member,
            role="member",
        )
        cls.document = Document.objects.create(
            company=cls.company,
            department=cls.department,
            uploaded_by=cls.admin,
            title="Input",
            original_filename="input.txt",
            status="ready",
            is_ai_ready=True,
            is_indexed=True,
        )
        cls.other_document = Document.objects.create(
            company=cls.other_company,
            uploaded_by=cls.foreign,
            title="Foreign",
            original_filename="foreign.txt",
            status="ready",
            is_ai_ready=True,
            is_indexed=True,
        )

    def setUp(self):
        self.enqueue_patch = patch.object(WorkflowEngineService, "enqueue")
        self.enqueue = self.enqueue_patch.start()
        self.addCleanup(patch.stopall)
        self.client.force_authenticate(self.admin)

    def definition(self, kind="human_review", principal=None, department=None):
        definition = WorkflowDefinitionService.create(
            self.admin,
            name="Reusable",
            code=f"process-{uuid.uuid4().hex[:10]}",
            department_id=department,
        )
        config = (
            {"assignment": principal or {"user": self.reviewer.pk}}
            if kind in {"human_review", "approval", "assignment"}
            else {}
        )
        if kind == "ai":
            config = {"instruction": "Review the supplied evidence."}
        steps = [
            {"key": "work", "name": "Work", "step_type": kind, "configuration": config},
            {"key": "done", "name": "Done", "step_type": "terminal"},
            {
                "key": "declined",
                "name": "Declined",
                "step_type": "terminal",
                "configuration": {"status": "rejected"},
            },
        ]
        outcomes = {
            "human_review": ["accepted", "rejected", "exception"],
            "approval": ["approved", "rejected", "exception"],
        }.get(kind, ["success"])
        transitions = [
            {
                "source": "work",
                "target": "declined" if outcome == "rejected" else "done",
                "outcome": outcome,
            }
            for outcome in outcomes
        ]
        if kind not in {"human_review", "approval"}:
            steps = steps[:2]
        version = WorkflowDefinitionService.publish(
            self.admin,
            definition,
            entry_step="work",
            steps=steps,
            transitions=transitions,
        )
        return definition, version

    def start(self, version=None, user=None, documents=None, key=None):
        if version is None:
            _, version = self.definition()
        return WorkflowEngineService.start(
            user or self.admin,
            version_uuid=version.uuid,
            document_uuids=documents or [str(self.document.uuid)],
            idempotency_key=key or uuid.uuid4().hex,
        )[0]

    def execute(self, run):
        run.refresh_from_db()
        result = WorkflowExecutorService.execute(
            run.company_id, str(run.uuid), run.revision
        )
        run.refresh_from_db()
        return result

    def command(self, run, action, user=None, key=None, revision=None, **payload):
        run.refresh_from_db()
        return WorkflowEngineService.command(
            user or self.admin,
            run,
            action=action,
            expected_revision=revision or run.revision,
            idempotency_key=key or uuid.uuid4().hex,
            **payload,
        )

    def test_company_cannot_view_or_start_foreign_workflow(self):
        _, version = self.definition()
        run = self.start(version)
        self.client.force_authenticate(self.foreign)
        self.assertEqual(
            self.client.get(f"/api/workflows/runs/{run.uuid}/").status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/api/workflows/versions/{version.uuid}/").status_code, 404
        )
        response = self.client.post(
            "/api/workflows/runs/",
            {
                "version": str(version.uuid),
                "documents": [str(self.document.uuid)],
                "idempotency_key": "foreign",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_department_isolation(self):
        definition, version = self.definition(department=self.private_department.pk)
        run = self.start(version)
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(
            self.client.get(
                f"/api/workflows/definitions/{definition.uuid}/"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/api/workflows/runs/{run.uuid}/").status_code, 404
        )
        with self.assertRaises(PermissionDenied):
            self.start(version, user=self.reviewer)

    def test_start_and_command_reject_company_and_status_injection(self):
        _, version = self.definition()
        data = {
            "version": str(version.uuid),
            "documents": [str(self.document.uuid)],
            "idempotency_key": "ownership",
            "company": self.other_company.pk,
        }
        self.assertEqual(
            self.client.post("/api/workflows/runs/", data, format="json").status_code,
            400,
        )
        run = self.start(version)
        self.assertEqual(
            self.client.patch(
                f"/api/workflows/runs/{run.uuid}/",
                {"status": "completed"},
                format="json",
            ).status_code,
            405,
        )
        self.assertEqual(
            self.client.post(
                f"/api/workflows/runs/{run.uuid}/commands/",
                {
                    "action": "cancel",
                    "expected_revision": run.revision,
                    "idempotency_key": "state",
                    "status": "completed",
                },
                format="json",
            ).status_code,
            400,
        )

    def test_unauthorized_transition_and_cancellation(self):
        run = self.start()
        self.execute(run)
        with self.assertRaises(PermissionDenied):
            self.command(run, "accept", user=self.member)
        with self.assertRaises(PermissionDenied):
            self.command(run, "cancel", user=self.member)

    def test_invalid_transition_is_safe(self):
        run = self.start()
        with self.assertRaises(ValidationError):
            self.command(run, "approve")
        run.refresh_from_db()
        self.assertEqual(run.status, "queued")

    def test_human_review_pauses_resumes_and_audits(self):
        run = self.start()
        self.execute(run)
        self.assertEqual(run.status, "review_required")
        step = run.step_runs.get(step=run.current_step)
        self.assertEqual(step.status, "waiting")
        self.assertEqual(step.assignment.user_id, self.reviewer.pk)
        self.command(run, "accept", user=self.reviewer, comment="Reviewed.")
        self.execute(run)
        self.assertEqual(run.status, "completed")
        self.assertIsNotNone(run.completed_at)
        self.assertTrue(
            WorkflowComment.objects.filter(run=run, body="Reviewed.").exists()
        )
        events = list(
            AuditLog.objects.filter(resource_uuid=run.uuid).values_list(
                "metadata", flat=True
            )
        )
        self.assertTrue(
            {
                "started",
                "step_started",
                "step_completed",
                "review_submitted",
                "completed",
            }.issubset({e["event"] for e in events})
        )

    def test_completed_workflow_cannot_restart(self):
        run = self.start()
        self.execute(run)
        self.command(run, "accept")
        self.execute(run)
        with self.assertRaises(ValidationError):
            self.command(run, "cancel")
        self.assertFalse(self.execute(run))
        self.assertEqual(run.status, "completed")

    def test_duplicate_execution_is_idempotent(self):
        run = self.start()
        revision = run.revision
        self.execute(run)
        self.assertFalse(
            WorkflowExecutorService.execute(run.company_id, str(run.uuid), revision)
        )
        self.assertEqual(
            WorkflowAssignment.objects.filter(step_run__run=run).count(), 1
        )
        self.assertEqual(run.step_runs.get(step=run.current_step).attempt, 1)

    def test_revision_conflict_prevents_double_progression(self):
        run = self.start()
        self.execute(run)
        old = run.revision
        self.command(run, "accept", key="first")
        with self.assertRaises(WorkflowConflict):
            self.command(run, "reject", revision=old, key="second")

    def test_start_idempotency_and_key_conflict(self):
        _, version = self.definition()
        first = self.start(version, key="stable")
        second, created = WorkflowEngineService.start(
            self.admin,
            version_uuid=version.uuid,
            document_uuids=[str(self.document.uuid)],
            idempotency_key="stable",
        )
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(created)
        with self.assertRaises(WorkflowConflict):
            WorkflowEngineService.start(
                self.admin,
                version_uuid=version.uuid,
                document_uuids=[str(self.document.uuid)],
                idempotency_key="stable",
                metadata={"other": True},
            )

    def test_command_idempotency_survives_progression(self):
        run = self.start()
        self.execute(run)
        revision = run.revision
        first = self.command(
            run, "accept", user=self.reviewer, revision=revision, key="same-command"
        )
        second = self.command(
            run, "accept", user=self.reviewer, revision=revision, key="same-command"
        )
        self.assertEqual(first.revision, second.revision)
        with self.assertRaises(WorkflowConflict):
            self.command(
                run,
                "accept",
                user=self.reviewer,
                revision=revision,
                key="same-command",
                comment="Changed",
            )

    def test_historical_version_remains_stable(self):
        definition, original = self.definition()
        run = self.start(original)
        replacement = WorkflowDefinitionService.publish(
            self.admin,
            definition,
            entry_step="end",
            steps=[{"key": "end", "name": "End", "step_type": "terminal"}],
            transitions=[],
        )
        self.assertEqual(replacement.number, 2)
        run.refresh_from_db()
        self.assertEqual(run.version_id, original.pk)
        self.assertEqual(run.current_step.key, "work")
        original.entry_step = "end"
        with self.assertRaises(DjangoValidationError):
            original.save()

    def test_cross_company_assignment_denied(self):
        run = self.start()
        self.execute(run)
        with self.assertRaises(ValidationError):
            self.command(run, "assign", assignment={"user": self.foreign.pk})
        with self.assertRaises(ValidationError):
            self.definition(principal={"user": self.foreign.pk})

    def test_assignment_requires_document_permission(self):
        unprivileged = User.objects.create_user(
            username="no-document", company=self.company
        )
        run = self.start()
        self.execute(run)
        with self.assertRaises(ValidationError):
            self.command(run, "assign", assignment={"user": unprivileged.pk})

    def test_role_department_assignment(self):
        _, version = self.definition(
            principal={"role": "reviewer", "department": self.department.pk}
        )
        run = self.start(version)
        self.execute(run)
        self.assertTrue(
            WorkflowPermissionService.can(
                self.reviewer, run, "review", run.step_runs.get(step=run.current_step)
            )
        )
        self.command(run, "accept", user=self.reviewer)

    def test_approval_reuses_existing_app_and_double_approval_is_denied(self):
        _, version = self.definition("approval")
        run = self.start(version)
        self.execute(run)
        self.assertEqual(run.status, "approval_required")
        approval = run.step_runs.get(step=run.current_step).approval
        self.assertIsInstance(approval, ApprovalRequest)
        ApprovalWorkflowService.approve(self.reviewer, approval)
        run.refresh_from_db()
        self.assertEqual(run.status, "queued")
        with self.assertRaises(ValidationError):
            ApprovalWorkflowService.approve(self.reviewer, approval)
        self.execute(run)
        self.assertEqual(run.status, "completed")

    def test_rejection_follows_configured_transition(self):
        _, version = self.definition("approval")
        run = self.start(version)
        self.execute(run)
        self.command(run, "reject", user=self.reviewer)
        self.execute(run)
        self.assertEqual(run.status, "rejected")

    def test_unassigned_reviewer_cannot_approve_workflow(self):
        other_reviewer = User.objects.create_user(
            username="other-reviewer", company=self.company, role="reviewer"
        )
        DocumentAccessPolicy.objects.create(
            document=self.document, user=other_reviewer, access_level="review"
        )
        _, version = self.definition("approval")
        run = self.start(version)
        self.execute(run)
        approval = run.step_runs.get(step=run.current_step).approval
        with self.assertRaises(PermissionDenied):
            ApprovalWorkflowService.approve(other_reviewer, approval)

    def test_exception_creation_and_resolution_are_auditable(self):
        _, version = self.definition("approval")
        run = self.start(version)
        self.execute(run)
        approval = run.step_runs.get(step=run.current_step).approval
        ApprovalWorkflowService.request_changes(
            self.reviewer, approval, reviewer_notes="Review again"
        )
        run.refresh_from_db()
        self.assertEqual(run.status, "exception")
        exception = run.exceptions.get(status="open")
        self.command(
            run,
            "resolve_exception",
            user=self.reviewer,
            exception=str(exception.uuid),
            resolution="Exception reviewed.",
        )
        exception.refresh_from_db()
        self.assertEqual(exception.status, "resolved")
        events = AuditLog.objects.filter(resource_uuid=run.uuid).values_list(
            "metadata", flat=True
        )
        self.assertTrue(
            {"exception_created", "exception_resolved"}.issubset(
                {e["event"] for e in events}
            )
        )

    def test_cancel_closes_waiting_approval(self):
        _, version = self.definition("approval")
        run = self.start(version)
        self.execute(run)
        approval = run.step_runs.get(step=run.current_step).approval
        self.command(run, "cancel")
        approval.refresh_from_db()
        self.assertEqual(approval.status, "cancelled")
        with self.assertRaises(ValidationError):
            ApprovalWorkflowService.approve(self.reviewer, approval)

    def test_linked_approval_cannot_be_directly_modified_or_deleted(self):
        _, version = self.definition("approval")
        run = self.start(version)
        self.execute(run)
        approval = run.step_runs.get(step=run.current_step).approval
        response = self.client.patch(
            f"/api/approvals/{approval.uuid}/",
            {"status": "cancelled"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.client.delete(f"/api/approvals/{approval.uuid}/").status_code,
            403,
        )

    def test_multidocument_authorization_and_revocation(self):
        second = Document.objects.create(
            company=self.company,
            uploaded_by=self.admin,
            title="Second",
            original_filename="second.txt",
            status="ready",
            is_ai_ready=True,
        )
        run = self.start(documents=[str(self.document.uuid), str(second.uuid)])
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(
            self.client.get(f"/api/workflows/runs/{run.uuid}/").status_code, 404
        )
        with self.assertRaises(PermissionDenied):
            self.start(
                documents=[str(self.document.uuid), str(self.other_document.uuid)]
            )
        self.document.soft_delete()
        self.assertFalse(self.execute(run))
        self.assertEqual(run.status, "failed")

    def test_inactive_and_deleted_definitions_cannot_start(self):
        definition, version = self.definition()
        definition.is_active = False
        definition.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            self.start(version)
        definition.is_active = True
        definition.soft_delete()
        with self.assertRaises(PermissionDenied):
            self.start(version)

    def test_definition_administration_is_protected(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(
            self.client.post(
                "/api/workflows/definitions/",
                {"name": "Bad", "code": "bad"},
                format="json",
            ).status_code,
            403,
        )

    def test_definition_type_filter_is_applied_before_pagination_and_keeps_scope(self):
        from apps.workflows.models import WorkflowDefinition

        invoice = WorkflowDefinitionService.create(
            self.admin, name="Invoice", code="filter-invoice", workflow_type="invoice"
        )
        WorkflowDefinitionService.create(
            self.admin,
            name="Contract",
            code="filter-contract",
            workflow_type="contract_review",
        )
        WorkflowDefinitionService.create(
            self.admin,
            name="Private invoice",
            code="filter-private-invoice",
            workflow_type="invoice",
            department_id=self.private_department.pk,
        )
        WorkflowDefinition.objects.create(
            company=self.other_company,
            name="Foreign invoice",
            code="filter-foreign-invoice",
            workflow_type="invoice",
        )
        self.client.force_authenticate(self.member)
        response = self.client.get("/api/workflows/definitions/?type=invoice")
        self.assertEqual(response.status_code, 200)
        rows = (
            response.data["results"]
            if isinstance(response.data, dict)
            else response.data
        )
        self.assertEqual([row["uuid"] for row in rows], [str(invoice.uuid)])

    def test_invalid_graph_and_config_rejected(self):
        definition = WorkflowDefinitionService.create(
            self.admin, name="Invalid", code="invalid"
        )
        with self.assertRaises(ValidationError):
            WorkflowDefinitionService.publish(
                self.admin,
                definition,
                entry_step="a",
                steps=[{"key": "a", "name": "A", "step_type": "notification"}],
                transitions=[{"source": "a", "target": "a", "outcome": "success"}],
            )
        with self.assertRaises(ValidationError):
            WorkflowDefinitionService.publish(
                self.admin,
                definition,
                entry_step="a",
                steps=[
                    {
                        "key": "a",
                        "name": "A",
                        "step_type": "terminal",
                        "configuration": {"url": "http://arbitrary"},
                    }
                ],
                transitions=[],
            )

    def test_notifications_are_scoped_and_content_free(self):
        run = self.start()
        self.execute(run)
        notes = Notification.objects.filter(resource_uuid=run.uuid)
        self.assertTrue(notes.filter(recipient=self.reviewer).exists())
        self.assertFalse(notes.exclude(company=self.company).exists())
        self.assertNotIn(
            self.document.title, " ".join(notes.values_list("message", flat=True))
        )

    def test_field_restricted_artifacts_comments_findings_are_not_exposed(self):
        run = self.start()
        step = run.step_runs.get(step=run.current_step)
        WorkflowArtifact.objects.create(
            run=run, step_run=step, kind="json", data={"salary": "SECRET"}
        )
        DocumentFieldPolicy.objects.create(
            document=self.document,
            field_key="salary",
            user=self.reviewer,
            can_view=False,
            can_use_ai=False,
        )
        self.client.force_authenticate(self.reviewer)
        for endpoint in ["artifacts", "comments", "findings", "exceptions"]:
            response = self.client.get(f"/api/workflows/runs/{run.uuid}/{endpoint}/")
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("SECRET", str(response.data))

    def test_ai_cannot_bypass_document_or_field_permissions(self):
        _, version = self.definition("ai")
        run = self.start(version)
        DocumentFieldPolicy.objects.create(
            document=self.document,
            field_key="salary",
            user=self.admin,
            can_view=True,
            can_use_ai=False,
        )
        with patch(
            "apps.workflows.services.workflow_ai_service.LLMGatewayService.generate_answer"
        ) as gateway:
            self.execute(run)
            gateway.assert_not_called()
        self.assertEqual(run.status, "failed")
        self.assertFalse(run.artifacts.exists())

    def test_ai_worker_revalidates_document_authorization(self):
        _, version = self.definition("ai")
        run = self.start(version)
        self.admin.is_active = False
        self.admin.save(update_fields=["is_active"])
        with patch(
            "apps.workflows.services.workflow_ai_service.LLMGatewayService.generate_answer"
        ) as gateway:
            self.execute(run)
            gateway.assert_not_called()
        self.assertEqual(run.status, "failed")

    def test_ai_schema_rejects_untrusted_output_and_fake_evidence(self):
        chunks = [{"chunk_uuid": "source"}]
        for answer in [
            "plain text",
            '{"status":"approved"}',
            '{"findings":[{"bad":true}]}',
            json.dumps(
                {
                    "findings": [
                        {
                            "category": "risk",
                            "severity": "high",
                            "title": "Risk",
                            "description": "Evidence",
                            "confidence": 0.8,
                            "evidence": ["fake"],
                        }
                    ]
                }
            ),
        ]:
            with self.assertRaises(ValidationError):
                WorkflowAIService.validate_output(answer, chunks)

    def test_ai_firewall_routing_and_evidence_persistence(self):
        from apps.ai_engine.models import LLMProviderConfig

        _, version = self.definition("ai")
        run = self.start(version)
        self.document.sensitivity_label = "restricted"
        self.document.save(update_fields=["sensitivity_label"])
        chunk_id = str(uuid.uuid4())
        chunks = [
            {
                "chunk_uuid": chunk_id,
                "document_uuid": str(self.document.uuid),
                "governance_revision": self.document.governance_revision,
                "content": "Authorized evidence.",
            }
        ]
        cloud = LLMProviderConfig(company=self.company, provider_type="openai")
        answer = {
            "findings": [
                {
                    "category": "generic",
                    "severity": "low",
                    "title": "Review",
                    "description": "Review evidence",
                    "confidence": 0.9,
                    "evidence": [chunk_id],
                }
            ]
        }
        with patch(
            "apps.workflows.services.workflow_ai_service.VectorSearchService.search",
            return_value=chunks,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMProviderConfigService.get_default_config_for_company",
            return_value=cloud,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMGatewayService.generate_answer",
            return_value={"answer": json.dumps(answer)},
        ) as gateway:
            self.execute(run)
            self.assertEqual(
                gateway.call_args.kwargs["provider_config"].provider_type, "ollama"
            )
            self.assertIn("Authorized evidence.", gateway.call_args.kwargs["prompt"])
            self.assertIn(chunk_id, gateway.call_args.kwargs["prompt"])
            self.assertIn(
                "Review the supplied evidence.", gateway.call_args.kwargs["prompt"]
            )
        self.assertEqual(run.status, "queued")
        artifact = run.artifacts.get()
        self.assertNotIn("Authorized evidence.", json.dumps(artifact.data))
        self.assertEqual(run.findings.get().evidence[0]["chunk_uuid"], chunk_id)
        self.assertTrue(
            AuditLog.objects.filter(
                resource_uuid=run.uuid,
                metadata__event="ai_policy_enforced",
                metadata__forced_local=True,
            ).exists()
        )

    def test_ai_firewall_block_is_permanent(self):
        from apps.data_firewall.models import FirewallScan

        _, version = self.definition("ai")
        run = self.start(version)
        FirewallScan.objects.create(
            document=self.document,
            decision="block_cloud_llm",
            requires_human_approval=True,
        )
        chunks = [
            {
                "chunk_uuid": str(uuid.uuid4()),
                "document_uuid": str(self.document.uuid),
                "content": "Secret",
            }
        ]
        with patch(
            "apps.workflows.services.workflow_ai_service.VectorSearchService.search",
            return_value=chunks,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMGatewayService.generate_answer"
        ) as gateway:
            self.execute(run)
            gateway.assert_not_called()
        self.assertEqual(run.status, "failed")
        self.assertFalse(self.execute(run))

    def test_queue_submission_runs_only_after_commit(self):
        run = self.start()
        from apps.workflows.services.workflow_engine_service import (
            WorkflowEngineService,
        )

        # Stop only the enqueue patch to exercise the actual publication hook.
        self.enqueue_patch.stop()
        with patch(
            "apps.workflows.tasks.workflow_tasks.execute_workflow.delay"
        ) as delay:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                WorkflowEngineService.enqueue(run)
                delay.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            delay.assert_called_once_with(run.company_id, str(run.uuid), run.revision)

    def extraction_version(self, *, with_rule=False, review_exception=False):
        from apps.document_processing.models import DocumentTextExtraction
        from apps.workflows.models import ExtractionTemplate, ExtractionTemplateField

        DocumentTextExtraction.objects.create(
            document=self.document, extracted_text="Reference: ORIGINAL"
        )
        template = ExtractionTemplate.objects.create(
            company=self.company, name="Generic reference", template_type="custom"
        )
        field = ExtractionTemplateField.objects.create(
            template=template,
            field_key="reference",
            label="Reference",
            regex_pattern=r"Reference:\s*(\w+)",
            is_required=True,
        )
        definition = WorkflowDefinitionService.create(
            self.admin,
            name="Reusable extraction",
            code="extract-" + uuid.uuid4().hex[:12],
        )
        steps = [
            {
                "key": "extract",
                "name": "Extract",
                "step_type": "extraction",
                "configuration": {"template": str(template.uuid)},
            },
            {"key": "done", "name": "Done", "step_type": "terminal"},
        ]
        edges = [
            {
                "source": "extract",
                "target": "rule" if with_rule else "done",
                "outcome": "success",
            }
        ]
        if with_rule:
            steps += [
                {
                    "key": "rule",
                    "name": "Validate reference",
                    "step_type": "validation",
                    "configuration": {
                        "source_step": "extract",
                        "path": "reference.value",
                        "operator": "eq",
                        "value": "ORIGINAL",
                    },
                },
                {
                    "key": "declined",
                    "name": "Declined",
                    "step_type": "terminal",
                    "configuration": {"status": "rejected"},
                },
            ]
            edges += [
                {"source": "rule", "target": "done", "outcome": "pass"},
                {"source": "rule", "target": "declined", "outcome": "fail"},
            ]
        if review_exception:
            edges += [{"source": "extract", "target": "done", "outcome": "exception"}]
        version = WorkflowDefinitionService.publish(
            self.admin, definition, entry_step="extract", steps=steps, transitions=edges
        )
        return version, template, field

    def test_generic_extraction_reuses_adapter_and_frozen_template(self):
        version, template, field = self.extraction_version(with_rule=True)
        run = self.start(version)
        field.regex_pattern = "DOES_NOT_MATCH"
        field.save(update_fields=["regex_pattern"])
        self.execute(run)
        self.assertEqual(run.artifacts.get().data["reference"]["value"], "ORIGINAL")
        self.assertEqual(
            run.step_runs.get(step__key="extract").extraction.template_id, template.pk
        )
        self.execute(run)
        self.assertEqual(run.current_step.key, "done")
        self.execute(run)
        self.assertEqual(run.status, "completed")

    def test_extraction_exception_correction_updates_authorized_artifact(self):
        version, _, _ = self.extraction_version(review_exception=True)
        from apps.document_processing.models import DocumentTextExtraction

        DocumentTextExtraction.objects.filter(document=self.document).update(
            extracted_text="No reference provided"
        )
        run = self.start(version)
        self.execute(run)
        self.assertEqual(run.status, "exception")
        extraction = run.step_runs.get(step=run.current_step).extraction
        field = extraction.result_fields.get()
        response = self.client.patch(
            f"/api/workflows/extraction-runs/{extraction.uuid}/fields/{field.uuid}/correct/",
            {"corrected_value": "REVIEWED"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.artifacts.get().data["reference"]["value"], "REVIEWED")
        self.assertEqual(
            self.client.post(
                f"/api/workflows/extraction-runs/{extraction.uuid}/rerun/"
            ).status_code,
            403,
        )
        exception = run.exceptions.get(status="open")
        self.command(
            run,
            "resolve_exception",
            exception=str(exception.uuid),
            resolution="Corrected by reviewer.",
        )
        self.execute(run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(
            self.client.patch(
                f"/api/workflows/extraction-runs/{extraction.uuid}/fields/{field.uuid}/correct/",
                {"corrected_value": "LATE"},
                format="json",
            ).status_code,
            403,
        )

    def test_rule_false_outcome_is_deterministic(self):
        version, _, _ = self.extraction_version(with_rule=True)
        from apps.document_processing.models import DocumentTextExtraction

        DocumentTextExtraction.objects.filter(document=self.document).update(
            extracted_text="Reference: DIFFERENT"
        )
        run = self.start(version)
        self.execute(run)
        self.execute(run)
        self.assertEqual(run.current_step.key, "declined")
        self.execute(run)
        self.assertEqual(run.status, "rejected")

    def test_task_retries_transient_failures_and_stops_at_bound(self):
        from apps.workflows.tasks.workflow_tasks import execute_workflow

        run = self.start()
        with patch.object(
            WorkflowExecutorService,
            "execute",
            side_effect=RuntimeError("PRIVATE SECRET"),
        ) as executor:
            result = execute_workflow.apply(
                args=[run.company_id, str(run.uuid), run.revision]
            )
        self.assertEqual(executor.call_count, 4)
        self.assertFalse(result.get())
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertNotIn("PRIVATE SECRET", run.error_message)
        self.assertEqual(run.step_runs.get(step=run.current_step).attempt, 4)

    def test_version_can_bound_execution_attempts(self):
        from apps.workflows.tasks.workflow_tasks import execute_workflow

        definition = WorkflowDefinitionService.create(
            self.admin, name="One attempt", code="one-attempt"
        )
        version = WorkflowDefinitionService.publish(
            self.admin,
            definition,
            entry_step="done",
            steps=[{"key": "done", "name": "Done", "step_type": "terminal"}],
            transitions=[],
            configuration={"max_attempts": 1},
        )
        run = self.start(version)
        with patch.object(
            WorkflowExecutorService, "execute", side_effect=RuntimeError("unavailable")
        ) as executor:
            result = execute_workflow.apply(
                args=[run.company_id, str(run.uuid), run.revision]
            )
        self.assertFalse(result.get())
        self.assertEqual(executor.call_count, 1)

    def test_task_does_not_retry_permanent_policy_failure(self):
        from apps.workflows.tasks.workflow_tasks import execute_workflow

        run = self.start()
        self.document.soft_delete()
        with patch.object(execute_workflow, "retry") as retry:
            result = execute_workflow.apply(
                args=[run.company_id, str(run.uuid), run.revision]
            )
            self.assertFalse(result.get())
            retry.assert_not_called()

    def test_queued_dispatcher_recovers_with_tenant_and_revision(self):
        from apps.workflows.tasks.workflow_tasks import dispatch_queued_workflows

        run = self.start()
        from datetime import timedelta
        from django.utils import timezone

        WorkflowRun.objects.filter(pk=run.pk).update(
            updated_at=timezone.now() - timedelta(seconds=60)
        )
        with patch(
            "apps.workflows.tasks.workflow_tasks.execute_workflow.delay"
        ) as delay:
            dispatch_queued_workflows()
            delay.assert_called_once_with(run.company_id, str(run.uuid), run.revision)

    def test_human_review_exception_path(self):
        run = self.start()
        self.execute(run)
        self.command(
            run,
            "request_changes",
            user=self.reviewer,
            comment="Needs exception review.",
        )
        run.refresh_from_db()
        self.assertEqual(run.status, "exception")
        exception = run.exceptions.get()
        self.command(run, "assign", assignment={"user": self.admin.pk})
        exception.refresh_from_db()
        self.assertEqual(exception.owner_id, self.admin.pk)
        self.command(
            run,
            "resolve_exception",
            exception=str(exception.uuid),
            resolution="Reviewed exception.",
        )
        self.execute(run)
        self.assertEqual(run.status, "completed")

    def test_existing_approval_api_progresses_group_assignment(self):
        _, version = self.definition(
            "approval", principal={"role": "reviewer", "department": self.department.pk}
        )
        run = self.start(version)
        self.execute(run)
        approval = run.step_runs.get(step=run.current_step).approval
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(
            self.client.get(f"/api/approvals/{approval.uuid}/").status_code, 200
        )
        self.assertEqual(
            self.client.post(
                f"/api/approvals/{approval.uuid}/approve/", {}, format="json"
            ).status_code,
            200,
        )
        run.refresh_from_db()
        self.assertEqual(run.status, "queued")

    def test_approval_all_document_relationships_are_enforced(self):
        other = Document.objects.create(
            company=self.company,
            uploaded_by=self.admin,
            title="Other",
            original_filename="other.txt",
            status="ready",
        )
        _, version = self.definition("approval", principal={"role": "reviewer"})
        run = self.start(version, documents=[str(self.document.uuid), str(other.uuid)])
        self.execute(run)
        approval = run.step_runs.get(step=run.current_step).approval
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(
            self.client.get(f"/api/approvals/{approval.uuid}/").status_code, 404
        )
        with self.assertRaises(PermissionDenied):
            ApprovalWorkflowService.approve(self.reviewer, approval)

    def test_definition_and_version_api_roundtrip(self):
        response = self.client.post(
            "/api/workflows/definitions/",
            {"name": "API process", "code": "api-process", "workflow_type": "generic"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        definition_uuid = response.data["uuid"]
        response = self.client.post(
            f"/api/workflows/definitions/{definition_uuid}/versions/",
            {
                "entry_step": "done",
                "steps": [{"key": "done", "name": "Done", "step_type": "terminal"}],
                "transitions": [],
                "configuration": {"max_attempts": 2},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        version_uuid = response.data["uuid"]
        self.assertEqual(
            self.client.get(f"/api/workflows/versions/{version_uuid}/").status_code, 200
        )
        data = {
            "version": version_uuid,
            "documents": [str(self.document.uuid)],
            "idempotency_key": "api-roundtrip",
        }
        response = self.client.post("/api/workflows/runs/", data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            self.client.post("/api/workflows/runs/", data, format="json").status_code,
            200,
        )
        run_uuid = response.data["uuid"]
        self.assertEqual(
            self.client.get(f"/api/workflows/runs/{run_uuid}/steps/").status_code, 200
        )
        self.assertEqual(
            self.client.post(
                f"/api/workflows/definitions/{definition_uuid}/activation/",
                {"is_active": False},
                format="json",
            ).status_code,
            200,
        )
        data["idempotency_key"] = "inactive-start"
        self.assertEqual(
            self.client.post("/api/workflows/runs/", data, format="json").status_code,
            403,
        )

    def test_external_document_cannot_enter_internal_workflow(self):
        external = Document.objects.create(
            company=self.company,
            uploaded_by=self.admin,
            title="External",
            original_filename="external.txt",
            scope="external",
            status="ready",
        )
        with self.assertRaises(PermissionDenied):
            self.start(documents=[str(external.uuid)])

    def test_stale_or_foreign_worker_delivery_is_noop(self):
        run = self.start()
        self.assertFalse(
            WorkflowExecutorService.execute(
                self.other_company.pk, str(run.uuid), run.revision
            )
        )
        self.assertFalse(
            WorkflowExecutorService.execute(
                self.company.pk, str(run.uuid), run.revision + 1
            )
        )
        run.refresh_from_db()
        self.assertEqual(run.status, "queued")

    def test_integration_requires_server_registered_adapter(self):
        definition = WorkflowDefinitionService.create(
            self.admin, name="Integration", code="integration"
        )
        with self.assertRaises(ValidationError):
            WorkflowDefinitionService.publish(
                self.admin,
                definition,
                entry_step="integration",
                steps=[
                    {
                        "key": "integration",
                        "name": "Integration",
                        "step_type": "integration",
                        "configuration": {"adapter": "http://untrusted"},
                    }
                ],
                transitions=[],
            )
        with patch.dict(
            WorkflowExecutorService.integration_adapters,
            {"safe_adapter": lambda **kwargs: {"reference": kwargs["idempotency_key"]}},
        ):
            version = WorkflowDefinitionService.publish(
                self.admin,
                definition,
                entry_step="integration",
                steps=[
                    {
                        "key": "integration",
                        "name": "Integration",
                        "step_type": "integration",
                        "configuration": {"adapter": "safe_adapter"},
                    },
                    {"key": "done", "name": "Done", "step_type": "terminal"},
                ],
                transitions=[
                    {"source": "integration", "target": "done", "outcome": "success"}
                ],
            )
            run = self.start(version)
            self.execute(run)
            self.assertEqual(
                run.artifacts.get().data["reference"],
                f"{run.uuid}:{run.version.steps.get(key='integration').uuid}",
            )

    def test_assignment_and_notification_steps_progress(self):
        _, version = self.definition("assignment")
        run = self.start(version)
        self.execute(run)
        self.assertEqual(run.status, "queued")
        self.assertTrue(run.step_runs.get(step__key="work").assignment)
        _, version = self.definition("notification")
        run = self.start(version)
        self.execute(run)
        self.assertEqual(run.status, "queued")
        self.assertTrue(
            Notification.objects.filter(
                resource_uuid=run.uuid, metadata__event="notification"
            ).exists()
        )

    def test_expired_document_removes_workflow_even_for_admin(self):
        from django.utils import timezone
        from datetime import timedelta

        run = self.start()
        Document.objects.filter(pk=self.document.pk).update(
            retention_start=timezone.now() - timedelta(days=366),
            retention_expires_at=timezone.now() - timedelta(days=1),
        )
        response = self.client.get("/api/workflows/runs/")
        self.assertNotIn(str(run.uuid), str(response.data))
        self.assertEqual(
            self.client.get(f"/api/workflows/runs/{run.uuid}/").status_code, 404
        )

    def test_inactive_department_removes_workflow_and_prevents_start(self):
        _, version = self.definition(department=self.department.pk)
        run = self.start(version)
        self.department.is_active = False
        self.department.save(update_fields=["is_active"])
        self.assertNotIn(
            str(run.uuid), str(self.client.get("/api/workflows/runs/").data)
        )
        with self.assertRaises(PermissionDenied):
            self.start(version)

    def test_finding_review_is_scoped_and_audited(self):
        from apps.workflows.models import WorkflowFinding

        run = self.start()
        self.execute(run)
        step = run.step_runs.get(step=run.current_step)
        finding = WorkflowFinding.objects.create(
            run=run,
            step_run=step,
            category="generic",
            severity="low",
            title="Check evidence",
        )
        endpoint = f"/api/workflows/runs/{run.uuid}/findings/{finding.uuid}/review/"
        self.client.force_authenticate(self.member)
        self.assertEqual(
            self.client.post(
                endpoint, {"status": "accepted"}, format="json"
            ).status_code,
            403,
        )
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(
            self.client.post(
                endpoint,
                {"status": "accepted", "owner": self.foreign.pk},
                format="json",
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                endpoint,
                {"status": "accepted", "owner": self.reviewer.pk},
                format="json",
            ).status_code,
            200,
        )
        finding.refresh_from_db()
        self.assertEqual(finding.review_state, "reviewed")
        self.assertTrue(
            AuditLog.objects.filter(
                resource_uuid=run.uuid, metadata__event="finding_reviewed"
            ).exists()
        )

    def test_transaction_rollback_does_not_enqueue_execution(self):
        from django.db import transaction

        run = self.start()
        self.enqueue_patch.stop()
        with patch(
            "apps.workflows.tasks.workflow_tasks.execute_workflow.delay"
        ) as delay:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        WorkflowEngineService.enqueue(run)
                        raise RuntimeError("Rollback")
                except RuntimeError:
                    pass
            delay.assert_not_called()

    def test_unconfigured_exception_path_rolls_back_approval_decision(self):
        _, version = self.definition("approval")
        # Publish a separate valid version without a request-changes path.
        definition = version.definition
        steps = [
            {
                "key": "approval",
                "name": "Approval",
                "step_type": "approval",
                "configuration": {"assignment": {"user": self.reviewer.pk}},
            },
            {"key": "done", "name": "Done", "step_type": "terminal"},
        ]
        version = WorkflowDefinitionService.publish(
            self.admin,
            definition,
            entry_step="approval",
            steps=steps,
            transitions=[
                {"source": "approval", "target": "done", "outcome": "approved"},
                {"source": "approval", "target": "done", "outcome": "rejected"},
            ],
        )
        run = self.start(version)
        self.execute(run)
        approval = run.step_runs.get(step=run.current_step).approval
        with self.assertRaises(ValidationError):
            ApprovalWorkflowService.request_changes(
                self.reviewer, approval, reviewer_notes="Changes"
            )
        approval.refresh_from_db()
        self.assertEqual(approval.status, "pending")
        run.refresh_from_db()
        self.assertEqual(run.status, "approval_required")

    def test_capabilities_follow_current_state_and_permissions(self):
        run = self.start()
        self.assertFalse(
            self.client.get(f"/api/workflows/runs/{run.uuid}/").data["capabilities"][
                "approve"
            ]
        )
        self.execute(run)
        self.client.force_authenticate(self.reviewer)
        capabilities = self.client.get(f"/api/workflows/runs/{run.uuid}/").data[
            "capabilities"
        ]
        self.assertTrue(capabilities["review"])
        self.assertTrue(capabilities["request_changes"])
        self.assertFalse(capabilities["assign"])
        self.assertFalse(capabilities["approve"])
        self.command(run, "accept", user=self.reviewer)
        self.execute(run)
        self.assertFalse(
            self.client.get(f"/api/workflows/runs/{run.uuid}/").data["capabilities"][
                "review"
            ]
        )

    def test_assignment_cannot_target_field_restricted_reviewer(self):
        run = self.start()
        self.execute(run)
        DocumentFieldPolicy.objects.create(
            document=self.document,
            field_key="salary",
            user=self.reviewer,
            can_view=False,
        )
        DocumentFieldPolicy.objects.create(
            document=self.document, field_key="salary", user=self.admin, can_view=True
        )
        with self.assertRaises(ValidationError):
            self.command(run, "assign", assignment={"user": self.reviewer.pk})

    def test_actual_private_gateway_receives_authorized_workflow_prompt(self):
        from apps.ai_engine.models import LLMProviderConfig

        _, version = self.definition("ai")
        run = self.start(version)
        self.document.sensitivity_label = "restricted"
        self.document.save(update_fields=["sensitivity_label"])
        source = str(uuid.uuid4())
        chunks = [
            {
                "chunk_uuid": source,
                "document_uuid": str(self.document.uuid),
                "content": "AUTHORIZED WORKFLOW EVIDENCE",
            }
        ]
        cloud = LLMProviderConfig(company=self.company, provider_type="openai")
        private = LLMProviderConfig(
            company=self.company,
            provider_type="ollama",
            endpoint_url="http://ollama:11434",
            model_name="test-model",
        )
        output = {
            "findings": [
                {
                    "category": "generic",
                    "severity": "low",
                    "title": "Review",
                    "description": "Review source",
                    "confidence": 0.8,
                    "evidence": [source],
                }
            ]
        }
        with patch(
            "apps.workflows.services.workflow_ai_service.VectorSearchService.search",
            return_value=chunks,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMProviderConfigService.get_default_config_for_company",
            return_value=cloud,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMProviderConfigService.get_private_config_for_company",
            return_value=private,
        ), patch("apps.ai_engine.services.llm_gateway_service.requests.post") as post:
            post.return_value.json.return_value = {
                "message": {"content": json.dumps(output)}
            }
            self.execute(run)
            self.assertEqual(post.call_args.args[0], "http://ollama:11434/api/chat")
            prompt = post.call_args.kwargs["json"]["messages"][1]["content"]
            self.assertIn("AUTHORIZED WORKFLOW EVIDENCE", prompt)
            self.assertIn(source, prompt)
            self.assertIn("Review the supplied evidence.", prompt)
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.findings.count(), 1)

    def test_cloud_prompt_contains_only_firewall_redacted_evidence(self):
        from apps.ai_engine.models import LLMProviderConfig

        _, version = self.definition("ai")
        run = self.start(version)
        self.company.allow_cloud_llm = True
        self.company.save(update_fields=["allow_cloud_llm"])
        self.document.sensitivity_label = "confidential"
        self.document.save(update_fields=["sensitivity_label"])
        source = str(uuid.uuid4())
        chunks = [
            {
                "chunk_uuid": source,
                "document_uuid": str(self.document.uuid),
                "content": "Contact test-person@example.com",
            }
        ]
        cloud = LLMProviderConfig(company=self.company, provider_type="openai")
        with patch(
            "apps.workflows.services.workflow_ai_service.VectorSearchService.search",
            return_value=chunks,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMProviderConfigService.get_default_config_for_company",
            return_value=cloud,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMGatewayService.generate_answer",
            return_value={"answer": '{"findings":[]}'},
        ) as gateway:
            self.execute(run)
            self.assertNotIn(
                "test-person@example.com", gateway.call_args.kwargs["prompt"]
            )
            self.assertEqual(
                gateway.call_args.kwargs["provider_config"].provider_type, "openai"
            )
        self.assertEqual(run.status, "queued")

    def test_private_provider_configuration_is_company_scoped(self):
        from apps.ai_engine.models import LLMProviderConfig
        from apps.ai_engine.services.llm_provider_config_service import (
            LLMProviderConfigService,
        )

        LLMProviderConfig.objects.create(
            company=self.other_company,
            name="Foreign private",
            provider_type="ollama",
            endpoint_url="http://ollama:11434",
            model_name="foreign-model",
        )
        local = LLMProviderConfig.objects.create(
            company=self.company,
            name="Own private",
            provider_type="ollama",
            endpoint_url="http://ollama:11434",
            model_name="own-model",
        )
        self.assertEqual(
            LLMProviderConfigService.get_private_config_for_company(self.company).pk,
            local.pk,
        )

    def test_model_http_failures_retry_without_logging_private_response(self):
        import requests
        from apps.ai_engine.models import LLMProviderConfig
        from apps.workflows.tasks.workflow_tasks import execute_workflow

        _, version = self.definition("ai")
        run = self.start(version)
        chunks = [
            {
                "chunk_uuid": str(uuid.uuid4()),
                "document_uuid": str(self.document.uuid),
                "content": "Authorized context",
            }
        ]
        private = LLMProviderConfig(
            company=self.company,
            provider_type="ollama",
            endpoint_url="http://ollama:11434",
            model_name="test-model",
        )
        response = requests.Response()
        response.status_code = 500
        response._content = b"PRIVATE RESPONSE CONTENT"
        error = requests.HTTPError("PRIVATE ERROR MESSAGE", response=response)
        with patch(
            "apps.workflows.services.workflow_ai_service.VectorSearchService.search",
            return_value=chunks,
        ), patch(
            "apps.workflows.services.workflow_ai_service.LLMProviderConfigService.get_default_config_for_company",
            return_value=private,
        ), patch(
            "apps.ai_engine.services.llm_gateway_service.requests.post",
            side_effect=error,
        ), self.assertLogs(
            "apps.ai_engine.services.llm_gateway_service", level="WARNING"
        ) as logs:
            result = execute_workflow.apply(
                args=[run.company_id, str(run.uuid), run.revision]
            )
        self.assertFalse(result.get())
        self.assertNotIn("PRIVATE RESPONSE CONTENT", " ".join(logs.output))
        self.assertNotIn("PRIVATE ERROR MESSAGE", " ".join(logs.output))
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.step_runs.get(step=run.current_step).attempt, 4)
        self.assertFalse(run.artifacts.exists())
