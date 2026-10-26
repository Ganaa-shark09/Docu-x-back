from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workspaces.models import Department, DepartmentMembership
from apps.workflows.models import WorkflowDefinition, WorkflowRunDocument


class WorkflowPermissionService:
    ACTIONS = {
        "view",
        "start",
        "cancel",
        "assign",
        "review",
        "approve",
        "reject",
        "resolve_exception",
        "comment",
        "administer",
    }

    @staticmethod
    def active(user):
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and user.company_id
            and not user.company.is_deleted
            and user.company.status == "active"
        )

    @classmethod
    def department_allowed(cls, user, department_id, manage=False):
        if (
            department_id
            and not Department.objects.filter(
                pk=department_id,
                company_id=user.company_id,
                is_active=True,
                is_deleted=False,
            ).exists()
        ):
            return False
        if DocumentPermissionService.is_company_admin(user):
            return True
        if department_id is None:
            return not manage
        memberships = DepartmentMembership.objects.filter(
            company_id=user.company_id,
            department_id=department_id,
            user=user,
            is_active=True,
            is_deleted=False,
            department__is_active=True,
            department__is_deleted=False,
        )
        if manage:
            memberships = memberships.filter(role="department_admin")
        return memberships.exists()

    @classmethod
    def definitions(cls, user):
        if not cls.active(user):
            return WorkflowDefinition.objects.none()
        qs = WorkflowDefinition.objects.filter(
            company_id=user.company_id, is_deleted=False
        )
        if not DocumentPermissionService.is_company_admin(user):
            qs = qs.filter(
                Q(department__isnull=True)
                | Q(
                    department_id__in=DocumentPermissionService.get_user_department_ids(
                        user
                    )
                )
            )
        return qs.filter(
            Q(department__isnull=True)
            | Q(department__is_active=True, department__is_deleted=False)
        )

    @classmethod
    def visible_runs(cls, user, queryset):
        if not cls.active(user):
            return queryset.none()
        queryset = queryset.filter(company_id=user.company_id, is_deleted=False)
        queryset = queryset.filter(
            Q(department__isnull=True)
            | Q(department__is_active=True, department__is_deleted=False)
        )
        if not DocumentPermissionService.is_company_admin(user):
            queryset = queryset.filter(
                Q(department__isnull=True)
                | Q(
                    department_id__in=DocumentPermissionService.get_user_department_ids(
                        user
                    )
                )
            )
        allowed = (
            DocumentPermissionService.get_accessible_document_queryset(
                user, scope="internal"
            )
            .filter(
                Q(retention_expires_at__isnull=True)
                | Q(retention_expires_at__gt=timezone.now())
            )
            .values("pk")
        )
        denied = WorkflowRunDocument.objects.filter(run_id=OuterRef("pk")).exclude(
            document_id__in=allowed
        )
        return queryset.annotate(_denied_document=Exists(denied)).filter(
            _denied_document=False
        )

    @classmethod
    def content_allowed(cls, user, run):
        allowed = cls.can(user, run, "view") and all(
            not FieldPermissionService.has_restrictions(user, doc, "view")
            for doc in run.documents.all()
        )
        if allowed and run.version.definition.workflow_type == "finance_audit":
            from uuid import UUID
            from apps.workflows.models.finance import FinanceAudit
            from apps.workflows.services.finance_audit_service import (
                FinanceAuditService,
            )

            try:
                audit_uuid = UUID(str(run.metadata.get("finance_audit_uuid")))
            except (ValueError, TypeError, AttributeError):
                return False
            audit = FinanceAudit.objects.filter(
                company_id=run.company_id,
                uuid=audit_uuid,
                is_deleted=False,
            ).first()
            if not audit:
                return False
            try:
                FinanceAuditService.authorize(user, audit)
            except PermissionDenied:
                return False
        if allowed and run.version.definition.workflow_type == "compliance":
            from apps.workflows.models.compliance import ComplianceAssessment
            from apps.workflows.services.compliance_assessment_service import (
                ComplianceAssessmentService,
            )

            assessment = ComplianceAssessment.objects.filter(
                run=run, company_id=run.company_id, is_deleted=False
            ).first()
            if not assessment:
                return False
            try:
                ComplianceAssessmentService.authorize(user, assessment)
            except (PermissionDenied, ValidationError):
                return False
        return allowed

    @classmethod
    def assigned(cls, user, step_run):
        if not step_run:
            return False
        assignment = getattr(step_run, "assignment", None)
        if assignment is None or assignment.is_deleted:
            return False
        if assignment.user_id:
            return assignment.user_id == user.pk
        if assignment.role and assignment.role != user.role:
            return False
        return (
            not assignment.department_id
            or DepartmentMembership.objects.filter(
                company_id=user.company_id,
                department_id=assignment.department_id,
                user=user,
                is_active=True,
                is_deleted=False,
                department__is_active=True,
                department__is_deleted=False,
            ).exists()
        )

    @classmethod
    def can(cls, user, run, action, step_run=None):
        if step_run is not None and step_run.run_id != run.pk:
            return False
        if (
            action not in cls.ACTIONS
            or not cls.active(user)
            or run.company_id != user.company_id
            or run.is_deleted
            or not cls.department_allowed(user, run.department_id)
        ):
            return False
        documents = list(run.documents.all())
        if not documents or any(
            doc.scope != "internal"
            or not DocumentPermissionService.can_action(user, doc, "view")
            for doc in documents
        ):
            return False
        if action == "view":
            return True
        if any(
            FieldPermissionService.has_restrictions(user, doc, "view")
            for doc in documents
        ):
            return False
        admin = DocumentPermissionService.is_company_admin(
            user
        ) or cls.department_allowed(user, run.department_id, manage=True)
        if action in {"assign", "administer"}:
            return admin
        if action == "cancel":
            return admin or (
                user.pk == run.initiated_by_id
                and all(
                    DocumentPermissionService.can_action(user, d, "edit")
                    for d in documents
                )
            )
        if action == "comment":
            return (
                admin or user.pk == run.initiated_by_id or cls.assigned(user, step_run)
            )
        if action in {"review", "approve", "reject", "resolve_exception"}:
            return (admin or cls.assigned(user, step_run)) and all(
                DocumentPermissionService.can_action(user, d, "correct")
                for d in documents
            )
        return False

    @classmethod
    def require(cls, user, run, action, step_run=None):
        if not cls.can(user, run, action, step_run):
            raise PermissionDenied(f"Workflow action '{action}' is not permitted.")
