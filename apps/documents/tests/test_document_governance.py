from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.ai_engine.models import (
    AIConversation,
    AIConversationDocument,
    AIMessage,
    LLMProviderConfig,
)
from apps.ai_engine.services.internal_conversation_memory_service import (
    InternalConversationMemoryService,
)
from apps.ai_engine.services.external_conversation_context_service import (
    ExternalConversationContextService,
)
from apps.ai_engine.services.message_visibility_service import MessageVisibilityService
from apps.audit.models import AuditLog
from apps.data_firewall.services import AIContextPolicyEnforcementService
from apps.documents.models import (
    Document,
    DocumentAccessPolicy,
    DocumentFieldPolicy,
    RetentionPolicy,
    SensitivityPolicy,
)
from apps.documents.services import DocumentPermissionService
from apps.documents.services.document_governance_service import (
    DocumentGovernanceService,
)
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.tenants.models import Company
from apps.workspaces.models import Department, DepartmentMembership
from apps.workflows.models import (
    ExtractionResultField,
    ExtractionRun,
    ExtractionTemplate,
)
from apps.workflows.services import ExtractionExportService


class DocumentGovernanceTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Governed", slug="governed")
        cls.other_company = Company.objects.create(name="Other", slug="other-governed")
        cls.admin = User.objects.create_user(
            username="governance-admin", company=cls.company, role="company_admin"
        )
        cls.member = User.objects.create_user(
            username="governance-member", company=cls.company, role="member"
        )
        cls.reviewer = User.objects.create_user(
            username="governance-reviewer", company=cls.company, role="reviewer"
        )
        cls.outsider = User.objects.create_user(
            username="governance-outsider", company=cls.company
        )
        cls.foreign = User.objects.create_user(
            username="governance-foreign",
            company=cls.other_company,
            role="company_admin",
        )
        cls.department = Department.objects.create(company=cls.company, name="Finance")
        cls.private_department = Department.objects.create(
            company=cls.company, name="Legal"
        )
        cls.membership = DepartmentMembership.objects.create(
            company=cls.company,
            department=cls.department,
            user=cls.member,
            role="viewer",
        )
        DepartmentMembership.objects.create(
            company=cls.company,
            department=cls.department,
            user=cls.reviewer,
            role="reviewer",
        )
        cls.document = Document.objects.create(
            company=cls.company,
            department=cls.department,
            uploaded_by=cls.admin,
            owner_user=cls.admin,
            title="Payroll",
            original_filename="payroll.txt",
            status="ready",
            is_ai_ready=True,
            is_indexed=True,
            is_ocr_completed=True,
        )
        cls.other_document = Document.objects.create(
            company=cls.other_company,
            uploaded_by=cls.foreign,
            title="Foreign",
            original_filename="foreign.txt",
            is_ai_ready=True,
        )
        cls.template = ExtractionTemplate.objects.create(
            name="Shared invoice", is_system=True
        )
        cls.extraction_run = ExtractionRun.objects.create(
            company=cls.company,
            document=cls.document,
            template=cls.template,
            requested_by=cls.reviewer,
            status="review_required",
            raw_output={"salary": {"value": "secret-payroll"}},
        )
        cls.field = ExtractionResultField.objects.create(
            extraction_run=cls.extraction_run,
            field_key="salary",
            label="Salary",
            extracted_value="secret-payroll",
            source_text="salary: secret-payroll",
            normalized_value={"value": "secret-payroll"},
        )

    def authenticate(self, user=None):
        self.client.force_authenticate(user=user or self.admin)

    def governance_url(self, document=None):
        return f"/api/documents/{(document or self.document).uuid}/governance/"

    def protect_salary(self, **kwargs):
        return DocumentFieldPolicy.objects.create(
            document=self.document,
            field_key="salary",
            user=kwargs.pop("user", self.admin),
            **kwargs,
        )

    def expire(self, hold=False):
        self.document.retention_start = timezone.now() - timedelta(days=3)
        self.document.retention_expires_at = timezone.now() - timedelta(days=1)
        self.document.legal_hold = hold
        self.document.save()

    def test_governance_is_tenant_scoped(self):
        self.authenticate()
        self.assertEqual(
            self.client.get(self.governance_url(self.other_document)).status_code, 404
        )
        self.assertEqual(
            self.client.patch(
                self.governance_url(self.other_document),
                {"expected_revision": 1, "legal_hold": True},
            ).status_code,
            404,
        )

    def test_governance_mutation_requires_admin(self):
        self.authenticate(self.member)
        self.assertEqual(self.client.get(self.governance_url()).status_code, 200)
        self.assertEqual(
            self.client.patch(
                self.governance_url(),
                {"expected_revision": 1, "owner_user": self.member.pk},
            ).status_code,
            403,
        )

    def test_governance_revision_conflict(self):
        self.authenticate()
        response = self.client.patch(
            self.governance_url(),
            {"expected_revision": 1, "legal_hold": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.patch(
            self.governance_url(),
            {"expected_revision": 1, "legal_hold": False},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.document.refresh_from_db()
        self.assertTrue(self.document.legal_hold)

    def test_governance_requires_revision(self):
        self.authenticate()
        self.assertEqual(
            self.client.patch(self.governance_url(), {"legal_hold": True}).status_code,
            400,
        )

    def test_foreign_owner_and_policy_references_rejected(self):
        self.authenticate()
        policy = RetentionPolicy.objects.create(
            company=self.other_company, name="Foreign", duration_days=10
        )
        for payload in [
            {"owner_user": self.foreign.pk},
            {"retention_policy": str(policy.uuid)},
        ]:
            with self.subTest(payload=payload):
                response = self.client.patch(
                    self.governance_url(),
                    {"expected_revision": 1, **payload},
                    format="json",
                )
                self.assertEqual(response.status_code, 400)

    def test_cross_company_owner_rejected_outside_api(self):
        self.document.owner_user = self.foreign
        with self.assertRaises(DjangoValidationError):
            self.document.save()

    def test_owner_transfer_preserves_uploader_without_implicit_authority(self):
        # Use an ordinary uploader to distinguish legacy uploader privileges.
        self.document.uploaded_by = self.outsider
        self.document.save()
        self.authenticate()
        response = self.client.patch(
            self.governance_url(),
            {"expected_revision": 1, "owner_user": self.reviewer.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.document.refresh_from_db()
        self.assertEqual(self.document.uploaded_by_id, self.outsider.pk)
        self.assertFalse(
            DocumentPermissionService.can_action(
                self.outsider, self.document, "administer"
            )
        )
        self.assertTrue(
            DocumentPermissionService.can_action(
                self.reviewer, self.document, "administer"
            )
        )

    def test_governance_change_and_audit_are_atomic(self):
        before = self.document.owner_user_id
        with patch(
            "apps.documents.services.document_governance_service.AuditLogService.log",
            side_effect=RuntimeError("audit unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                DocumentGovernanceService.update(
                    self.admin,
                    self.document,
                    {"owner_user": self.reviewer},
                    expected_revision=1,
                )
        self.document.refresh_from_db()
        self.assertEqual(self.document.owner_user_id, before)
        self.assertEqual(self.document.governance_revision, 1)

    def test_audit_has_safe_before_after_values(self):
        DocumentGovernanceService.update(
            self.admin, self.document, {"legal_hold": True}
        )
        audit = AuditLog.objects.get(event_type="document_governance_changed")
        self.assertEqual(audit.company_id, self.company.pk)
        self.assertEqual(audit.user_id, self.admin.pk)
        self.assertIn("before", audit.metadata)
        self.assertNotIn("secret-payroll", str(audit.metadata))

    def test_retention_snapshot_survives_policy_edit(self):
        policy = RetentionPolicy.objects.create(
            company=self.company, name="Invoices", duration_days=30
        )
        updated = DocumentGovernanceService.update(
            self.admin, self.document, {"retention_policy": policy}
        )
        self.assertEqual(updated.retention_duration_days, 30)
        self.assertEqual(
            updated.retention_expires_at, updated.retention_start + timedelta(days=30)
        )
        policy.duration_days = 90
        policy.save()
        updated.refresh_from_db()
        self.assertEqual(updated.retention_duration_days, 30)

    def test_retention_start_cannot_be_null_or_future(self):
        for value in (None, timezone.now() + timedelta(days=1)):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as caught:
                    DocumentGovernanceService.update(
                        self.admin, self.document, {"retention_start": value}
                    )
                self.assertEqual(caught.exception.__class__.__name__, "ValidationError")

    def test_retention_prevents_early_deletion(self):
        self.authenticate()
        response = self.client.delete(f"/api/documents/{self.document.uuid}/")
        self.assertEqual(response.status_code, 403)

    def test_legal_hold_prevents_expired_deletion_and_ai(self):
        self.expire(hold=True)
        self.authenticate()
        self.assertEqual(
            self.client.delete(f"/api/documents/{self.document.uuid}/").status_code, 403
        )
        self.assertFalse(DocumentGovernanceService.deletion_eligible(self.document))
        self.assertFalse(
            DocumentPermissionService.can_action(self.admin, self.document, "ai")
        )
        self.assertEqual(
            self.client.get(self.governance_url()).data["retention_status"],
            "legal_hold",
        )

    def test_expired_document_can_be_disposed_by_admin(self):
        self.expire()
        self.authenticate()
        self.assertEqual(
            self.client.delete(f"/api/documents/{self.document.uuid}/").status_code, 204
        )
        self.document.refresh_from_db()
        self.assertTrue(self.document.is_deleted)

    def test_expired_document_content_is_inaccessible(self):
        self.expire()
        self.authenticate(self.member)
        self.assertEqual(
            self.client.get(f"/api/documents/{self.document.uuid}/").status_code, 404
        )
        self.authenticate()
        self.assertEqual(
            self.client.get(f"/api/documents/{self.document.uuid}/").status_code, 403
        )
        self.assertEqual(self.client.get(self.governance_url()).status_code, 200)

    def test_another_department_does_not_grant_access(self):
        self.document.department = self.private_department
        self.document.save()
        self.assertFalse(
            DocumentPermissionService.can_action(self.member, self.document, "view")
        )

    def test_member_cannot_self_enroll_as_department_admin(self):
        self.authenticate(self.member)
        response = self.client.post(
            "/api/workspaces/memberships/",
            {
                "department": self.private_department.pk,
                "user": self.member.pk,
                "role": "department_admin",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            DepartmentMembership.objects.filter(
                department=self.private_department, user=self.member
            ).exists()
        )

    def test_member_cannot_promote_existing_membership_or_edit_department(self):
        self.authenticate(self.member)
        self.assertIn(
            self.client.patch(
                f"/api/workspaces/memberships/{self.membership.uuid}/",
                {"role": "department_admin"},
            ).status_code,
            (403, 404),
        )
        self.assertEqual(
            self.client.patch(
                f"/api/workspaces/departments/{self.department.uuid}/",
                {"name": "Changed"},
            ).status_code,
            403,
        )

    def test_company_admin_can_manage_memberships_with_audit(self):
        self.authenticate()
        response = self.client.post(
            "/api/workspaces/memberships/",
            {
                "department": self.private_department.pk,
                "user": self.outsider.pk,
                "role": "reviewer",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(
            AuditLog.objects.filter(resource_type="departmentmembership").exists()
        )

    def test_member_and_company_admin_cannot_mutate_system_template(self):
        for user in (self.member, self.admin):
            self.authenticate(user)
            for method, path, payload in [
                (
                    "patch",
                    f"/api/workflows/extraction-templates/{self.template.uuid}/",
                    {"name": "Corrupt"},
                ),
                (
                    "post",
                    f"/api/workflows/extraction-templates/{self.template.uuid}/fields/",
                    {"field_key": "private", "label": "Private"},
                ),
            ]:
                with self.subTest(user=user.username, method=method):
                    self.assertEqual(
                        getattr(self.client, method)(
                            path, payload, format="json"
                        ).status_code,
                        403,
                    )

    def test_company_admin_can_edit_own_template_but_not_other_tenant(self):
        own = ExtractionTemplate.objects.create(company=self.company, name="Own")
        other = ExtractionTemplate.objects.create(
            company=self.other_company, name="Other"
        )
        self.authenticate()
        self.assertEqual(
            self.client.patch(
                f"/api/workflows/extraction-templates/{own.uuid}/", {"name": "Updated"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/workflows/extraction-templates/{other.uuid}/",
                {"name": "Updated"},
            ).status_code,
            404,
        )

    def test_viewer_cannot_correct_extraction(self):
        self.authenticate(self.member)
        response = self.client.patch(
            f"/api/workflows/extraction-runs/{self.extraction_run.uuid}/fields/{self.field.uuid}/correct/",
            {"corrected_value": "tampered"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.field.refresh_from_db()
        self.assertFalse(self.field.is_corrected)

    def test_reviewer_can_correct_with_content_free_audit(self):
        self.authenticate(self.reviewer)
        response = self.client.patch(
            f"/api/workflows/extraction-runs/{self.extraction_run.uuid}/fields/{self.field.uuid}/correct/",
            {"corrected_value": "new-secret"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        audit = AuditLog.objects.get(resource_type="extraction_result_field")
        self.assertNotIn("new-secret", str(audit.metadata))

    def test_approved_extraction_cannot_be_corrected(self):
        self.extraction_run.status = "approved"
        self.extraction_run.save()
        self.authenticate(self.reviewer)
        response = self.client.patch(
            f"/api/workflows/extraction-runs/{self.extraction_run.uuid}/fields/{self.field.uuid}/correct/",
            {"corrected_value": "new-secret"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_view_access_does_not_allow_export(self):
        self.authenticate(self.member)
        response = self.client.get(
            f"/api/workflows/extraction-runs/{self.extraction_run.uuid}/export/"
        )
        self.assertEqual(response.status_code, 403)

    def test_field_restriction_denies_export_even_to_document_admin(self):
        self.protect_salary(can_view=True, can_use_ai=True, can_export=False)
        self.assertFalse(
            DocumentPermissionService.can_action(self.admin, self.document, "export")
        )
        with self.assertRaises(PermissionDenied):
            ExtractionExportService.export(self.admin, self.extraction_run, "json")

    def test_field_ai_restriction_removes_document_before_retrieval(self):
        self.protect_salary(can_view=True, can_use_ai=False, can_export=True)
        self.assertFalse(
            DocumentPermissionService.get_accessible_document_queryset(
                self.admin, require_ai=True
            )
            .filter(pk=self.document.pk)
            .exists()
        )
        result = AIContextPolicyEnforcementService.enforce(
            self.admin,
            None,
            [{"document_uuid": str(self.document.uuid), "content": "secret-payroll"}],
        )
        self.assertEqual(result["chunks"], [])
        self.assertEqual(result["blocked_count"], 1)

    def test_restricted_fields_are_removed_from_workflow_evidence_and_raw_output(self):
        self.protect_salary(can_view=True, can_use_ai=True, can_export=True)
        self.authenticate(self.member)
        response = self.client.get(
            f"/api/workflows/extraction-runs/{self.extraction_run.uuid}/"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("secret-payroll", str(response.data))
        self.assertEqual(response.data["raw_output"], {})

    def test_raw_download_and_preview_are_denied_for_restricted_field(self):
        self.protect_salary(can_view=True, can_use_ai=True, can_export=True)
        self.authenticate(self.member)
        for action in ("download", "preview"):
            self.assertEqual(
                self.client.get(
                    f"/api/documents/{self.document.uuid}/{action}/"
                ).status_code,
                403,
            )

    def test_field_permissions_default_deny_and_explicit_denial_wins(self):
        self.protect_salary(can_view=True, can_use_ai=True, can_export=True)
        self.assertFalse(
            FieldPermissionService.field_allowed(self.member, self.document, "salary")
        )
        self.protect_salary(can_view=False, can_use_ai=False, can_export=False)
        self.assertFalse(
            FieldPermissionService.field_allowed(self.admin, self.document, "salary")
        )

    def test_field_policy_management_is_tenant_scoped_and_audited(self):
        self.authenticate()
        response = self.client.post(
            f"/api/documents/{self.document.uuid}/field-policies/",
            {"field_key": "salary", "user": self.foreign.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            f"/api/documents/{self.document.uuid}/field-policies/",
            {"field_key": "salary", "user": self.admin.pk, "can_view": True},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(
            AuditLog.objects.filter(event_type="document_permission_changed").exists()
        )

    def test_explicit_ai_denial_overrides_department_access(self):
        DocumentAccessPolicy.objects.create(
            document=self.document, user=self.member, can_use_ai=False
        )
        self.assertFalse(
            DocumentPermissionService.can_action(self.member, self.document, "ai")
        )

    def test_configurable_sensitivity_influences_routing_and_exports(self):
        SensitivityPolicy.objects.create(
            company=self.company,
            code="payroll-private",
            name="Payroll",
            security_class="restricted",
            allow_ai=True,
            allow_cloud=False,
            allow_export=False,
        )
        self.document.sensitivity_label = "payroll-private"
        self.document.save()
        provider = LLMProviderConfig(company=self.company, provider_type="openai")
        result = AIContextPolicyEnforcementService.enforce(
            self.admin,
            provider,
            [
                {
                    "document_uuid": str(self.document.uuid),
                    "content": "Company confidential evidence",
                }
            ],
        )
        self.assertTrue(result["forced_local"])
        self.assertIsNone(result["provider_config"])
        self.assertFalse(
            DocumentPermissionService.can_action(self.admin, self.document, "export")
        )

    def test_sensitivity_minimum_access_applies_to_list_and_action(self):
        SensitivityPolicy.objects.create(
            company=self.company,
            code="internal",
            name="Internal",
            security_class="internal",
            minimum_access="review",
        )
        self.assertFalse(
            DocumentPermissionService.can_action(self.member, self.document, "view")
        )
        self.assertTrue(
            DocumentPermissionService.can_action(self.reviewer, self.document, "view")
        )

    def test_policy_admin_api_rejects_downgrades_and_tenant_injection(self):
        self.authenticate()
        response = self.client.post(
            "/api/documents/sensitivity-policies/",
            {
                "code": "restricted",
                "name": "Weak",
                "security_class": "public",
                "minimum_access": "view",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/documents/retention-policies/",
            {
                "company": self.other_company.pk,
                "name": "Retention",
                "duration_days": 10,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            RetentionPolicy.objects.get(uuid=response.data["uuid"]).company_id,
            self.company.pk,
        )

    def test_policy_apis_are_tenant_isolated_and_admin_only(self):
        policy = RetentionPolicy.objects.create(
            company=self.other_company, name="Other", duration_days=10
        )
        self.authenticate()
        self.assertEqual(
            self.client.get(
                f"/api/documents/retention-policies/{policy.uuid}/"
            ).status_code,
            404,
        )
        self.authenticate(self.member)
        self.assertEqual(
            self.client.get("/api/documents/retention-policies/").status_code, 403
        )

    def test_company_suspension_blocks_document_access(self):
        self.company.status = "suspended"
        self.company.save()
        self.assertFalse(
            DocumentPermissionService.can_action(self.admin, self.document, "view")
        )

    def test_existing_document_response_keeps_legacy_fields(self):
        self.authenticate(self.member)
        response = self.client.get(f"/api/documents/{self.document.uuid}/")
        self.assertEqual(response.status_code, 200, response.data)
        for key in (
            "uuid",
            "sensitivity_label",
            "status",
            "versions",
            "processing_jobs",
            "text_extraction",
        ):
            self.assertIn(key, response.data)
        self.assertIn("governance", response.data)

    def make_answer(self):
        conversation = AIConversation.objects.create(
            company=self.company, user=self.member, scope="internal"
        )
        answer = AIMessage.objects.create(
            conversation=conversation,
            role="assistant",
            content="secret-payroll",
            retrieved_sources=[
                {
                    "document_uuid": str(self.document.uuid),
                    "content_preview": "secret-payroll",
                }
            ],
        )
        return conversation, answer

    def test_revocation_hides_stored_answer_and_source_card(self):
        conversation, answer = self.make_answer()
        self.assertTrue(MessageVisibilityService.visible(answer, self.member))
        self.membership.soft_delete()
        self.authenticate(self.member)
        response = self.client.get(f"/api/ai/conversations/{conversation.uuid}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("secret-payroll", str(response.data))
        history = InternalConversationMemoryService.build_history_messages(
            conversation, self.member
        )
        self.assertNotIn("secret-payroll", str(history))

    def test_summary_provenance_is_transitive_and_revocable(self):
        conversation, answer = self.make_answer()
        summary = AIMessage.objects.create(
            conversation=conversation,
            role="assistant",
            content="summary secret-payroll",
            metadata={"intent": "conversation_summary"},
        )
        self.assertIn(
            str(self.document.uuid), summary.metadata["document_dependencies"]
        )
        self.membership.soft_delete()
        self.assertFalse(MessageVisibilityService.visible(summary, self.member))

    def test_governance_revision_invalidates_old_derived_content(self):
        conversation, answer = self.make_answer()
        DocumentGovernanceService.update(
            self.admin, self.document, {"legal_hold": True}
        )
        self.assertFalse(MessageVisibilityService.visible(answer, self.member))

    def test_external_history_does_not_reuse_deleted_attachment_content(self):
        conversation = AIConversation.objects.create(
            company=self.company, user=self.member, scope="external"
        )
        document = Document.objects.create(
            company=self.company,
            uploaded_by=self.member,
            scope="external",
            title="Upload",
            original_filename="upload.txt",
            is_ai_ready=True,
        )
        AIConversationDocument.objects.create(
            company=self.company,
            conversation=conversation,
            document=document,
            uploaded_by=self.member,
        )
        AIMessage.objects.create(
            conversation=conversation,
            role="assistant",
            content="external-secret",
            retrieved_sources=[{"document_uuid": str(document.uuid)}],
        )
        document.soft_delete()
        self.assertNotIn(
            "external-secret",
            str(
                ExternalConversationContextService.build_history_messages(conversation)
            ),
        )

    def test_external_history_never_accepts_internal_document_context(self):
        conversation = AIConversation.objects.create(
            company=self.company, user=self.member, scope="external"
        )
        answer = AIMessage.objects.create(
            conversation=conversation,
            role="assistant",
            content="internal-secret",
            retrieved_sources=[{"document_uuid": str(self.document.uuid)}],
        )
        self.assertFalse(MessageVisibilityService.visible(answer, self.member))

    def test_field_restriction_invalidates_prior_ai_answer(self):
        conversation, answer = self.make_answer()
        self.protect_salary(can_view=True, can_use_ai=False, can_export=True)
        self.assertFalse(MessageVisibilityService.visible(answer, self.member))

    def test_legacy_unprovenanced_summary_is_hidden(self):
        conversation, answer = self.make_answer()
        # Represents a summary persisted before governance provenance existed.
        AIMessage.objects.filter(pk=answer.pk).update(
            retrieved_sources=[], metadata={"intent": "conversation_summary"}
        )
        answer.refresh_from_db()
        self.assertFalse(MessageVisibilityService.visible(answer, self.member))

    def test_spreadsheet_export_neutralizes_formula_values(self):
        self.assertEqual(
            ExtractionExportService.safe_cell("=HYPERLINK('unsafe')"),
            "'=HYPERLINK('unsafe')",
        )
        self.assertEqual(ExtractionExportService.safe_cell(123), 123)

    def test_governance_revision_at_retrieval_is_not_replaced_at_save(self):
        revision = self.document.governance_revision
        DocumentGovernanceService.update(
            self.admin, self.document, {"legal_hold": True}
        )
        conversation = AIConversation.objects.create(
            company=self.company, user=self.member
        )
        answer = AIMessage.objects.create(
            conversation=conversation,
            role="assistant",
            content="secret-payroll",
            retrieved_sources=[
                {
                    "document_uuid": str(self.document.uuid),
                    "governance_revision": revision,
                }
            ],
        )
        self.assertFalse(MessageVisibilityService.visible(answer, self.member))

    def test_chat_response_rechecks_access_after_generation(self):
        conversation, answer = self.make_answer()
        self.membership.soft_delete()
        self.authenticate(self.member)
        with patch(
            "apps.ai_engine.views.ai_chat_views.RAGChatService.chat",
            return_value={
                "conversation": conversation,
                "assistant_message": answer,
            },
        ):
            response = self.client.post(
                "/api/ai/chat/", {"message": "Payroll?"}, format="json"
            )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertNotIn("secret-payroll", str(response.data))

    def test_unknown_sensitivity_is_excluded_from_ai_context(self):
        self.document.sensitivity_label = "unknown-policy"
        self.document.save()
        self.assertFalse(
            DocumentPermissionService.get_accessible_document_queryset(
                self.admin,
                require_ai=True,
            )
            .filter(pk=self.document.pk)
            .exists()
        )

    def test_revoked_requester_cannot_execute_pending_extraction(self):
        from apps.workflows.services import StructuredExtractionService

        self.extraction_run.status = "pending"
        self.extraction_run.save()
        self.reviewer.is_active = False
        self.reviewer.save()
        StructuredExtractionService.run_extraction(self.extraction_run)
        self.extraction_run.refresh_from_db()
        self.assertEqual(self.extraction_run.status, "failed")
        self.assertEqual(self.field.extracted_value, "secret-payroll")

    def test_extraction_retry_cannot_overwrite_approved_results(self):
        from apps.workflows.services import StructuredExtractionService

        self.extraction_run.status = "approved"
        self.extraction_run.save()
        StructuredExtractionService.run_extraction(self.extraction_run)
        self.extraction_run.refresh_from_db()
        self.assertEqual(self.extraction_run.status, "approved")
        self.assertEqual(self.extraction_run.result_fields.count(), 1)

    def test_retention_backfill_preserves_uploader_and_legal_hold(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection
        from types import SimpleNamespace

        Document.objects.filter(pk=self.document.pk).update(
            retention_start=None,
            retention_duration_days=None,
            retention_expires_at=None,
            owner_user=None,
            legal_hold=True,
        )
        migration = import_module(
            "apps.documents.migrations.0005_backfill_document_retention"
        )
        migration.backfill_retention(apps, SimpleNamespace(connection=connection))
        self.document.refresh_from_db()
        self.assertIsNone(self.document.owner_user_id)
        self.assertEqual(self.document.uploaded_by_id, self.admin.pk)
        self.assertTrue(self.document.legal_hold)
        self.assertEqual(
            self.document.retention_expires_at,
            self.document.created_at + timedelta(days=self.company.data_retention_days),
        )

    def test_debug_media_is_not_publicly_served(self):
        from django.test import override_settings

        with override_settings(DEBUG=True):
            response = self.client.get("/media/documents/payroll.txt")
        self.assertEqual(response.status_code, 404)

    def test_approved_export_succeeds_with_document_and_field_permissions(self):
        from apps.approvals.models import ApprovalRequest

        approval = ApprovalRequest.objects.create(
            company=self.company,
            document=self.document,
            requested_by=self.admin,
            title="Payroll approval",
            status="approved",
            reviewed_payload={"salary": {"value": "approved-payroll"}},
        )
        self.extraction_run.status = "approved"
        self.extraction_run.approval_request_uuid = approval.uuid
        self.extraction_run.save()
        self.protect_salary(can_view=True, can_use_ai=True, can_export=True)
        for export_format in ("json", "csv", "xlsx"):
            with self.subTest(export_format=export_format):
                response = ExtractionExportService.export(
                    self.admin, self.extraction_run, export_format
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(
            AuditLog.objects.filter(
                resource_uuid=self.extraction_run.uuid,
                event_type=AuditLog.EventType.EXTRACTION_EXPORTED,
            ).count(),
            3,
        )

    def test_approved_extraction_rerun_is_rejected_without_dispatch(self):
        self.extraction_run.status = "approved"
        self.extraction_run.save()
        self.authenticate(self.reviewer)
        with patch(
            "apps.workflows.tasks.run_structured_extraction_task.delay"
        ) as dispatch:
            response = self.client.post(
                f"/api/workflows/extraction-runs/{self.extraction_run.uuid}/rerun/"
            )
        self.assertEqual(response.status_code, 409, response.data)
        dispatch.assert_not_called()
        self.assertEqual(self.extraction_run.result_fields.count(), 1)

    def test_unprovenanced_legacy_followup_answer_is_hidden(self):
        conversation, answer = self.make_answer()
        AIMessage.objects.filter(pk=answer.pk).update(retrieved_sources=[], metadata={})
        answer.refresh_from_db()
        self.assertFalse(MessageVisibilityService.visible(answer, self.member))
