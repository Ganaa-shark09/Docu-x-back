from django.db import transaction
from django.db.models import Q, Max
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit.services import AuditLogService
from apps.documents.services import DocumentPermissionService
from apps.tenants.models import Company
from apps.workflows.models.compliance import (
    ComplianceFramework,
    ComplianceFrameworkVersion,
    ComplianceRequirement,
    ComplianceControl,
    ComplianceControlMapping,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services import compliance_schema as schema
from apps.workflows.services.finance_audit_service import digest
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import WorkflowConflict


def event(user, resource, name):
    AuditLogService.log(
        company=user.company,
        user=user,
        event_type="workflow_event",
        action="Compliance " + name,
        resource_type="compliance",
        resource_uuid=resource.uuid,
        metadata={"event": name, "revision": getattr(resource, "revision", None)},
    )


class ComplianceLibraryService:
    @staticmethod
    def frameworks(user):
        if not WorkflowPermissionService.active(user):
            return ComplianceFramework.objects.none()
        return ComplianceFramework.objects.filter(
            Q(company_id=user.company_id) | Q(company__isnull=True), is_deleted=False
        ).select_related("active_version")

    @staticmethod
    def controls(user):
        if not WorkflowPermissionService.active(user):
            return ComplianceControl.objects.none()
        qs = ComplianceControl.objects.filter(
            company_id=user.company_id, is_deleted=False
        ).filter(
            Q(department__isnull=True)
            | Q(department__is_active=True, department__is_deleted=False)
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
        return qs

    @staticmethod
    @transaction.atomic
    def create_framework(
        user, *, code, name, description="", source="", jurisdiction="", draft
    ):
        WorkflowDefinitionService.require_admin(user)
        Company.objects.select_for_update().get(pk=user.company_id)
        code = schema.code(code)
        if ComplianceFramework.objects.filter(
            company_id=user.company_id, code=code
        ).exists():
            raise ValidationError("Framework code already exists.")
        result = ComplianceFramework.objects.create(
            company=user.company,
            code=code,
            name=schema.text(name),
            description=schema.text(description, 4000, True),
            source=schema.text(source, 500, True),
            jurisdiction=schema.text(jurisdiction, 100, True),
            draft=schema.framework(draft),
        )
        event(user, result, "framework_created")
        return result

    @classmethod
    def lock_framework(cls, user, framework, revision):
        WorkflowDefinitionService.require_admin(user)
        framework = ComplianceFramework.objects.select_for_update().get(pk=framework.pk)
        if framework.company_id != user.company_id or framework.is_deleted:
            raise PermissionDenied("System and foreign frameworks cannot be changed.")
        if framework.revision != revision:
            raise WorkflowConflict()
        return framework

    @classmethod
    @transaction.atomic
    def draft(cls, user, framework, *, revision, draft):
        framework = cls.lock_framework(user, framework, revision)
        framework.draft = schema.framework(draft)
        framework.revision += 1
        framework.save(update_fields=["draft", "revision", "updated_at"])
        event(user, framework, "draft_updated")
        return framework

    @classmethod
    @transaction.atomic
    def publish(cls, user, framework, *, revision):
        framework = cls.lock_framework(user, framework, revision)
        draft = schema.framework(framework.draft)
        number = (framework.versions.aggregate(n=Max("number"))["n"] or 0) + 1
        metadata = {
            "code": framework.code,
            "name": framework.name,
            "description": framework.description,
            "source": framework.source,
            "jurisdiction": framework.jurisdiction,
            "policy": draft["policy"],
        }
        version = ComplianceFrameworkVersion.objects.create(
            framework=framework,
            number=number,
            metadata=metadata,
            digest=digest(
                {"metadata": metadata, "requirements": draft["requirements"]}
            ),
            published_by=user,
        )
        pending = {r["code"]: r for r in draft["requirements"]}
        created = {}
        while pending:
            for key, row in list(pending.items()):
                if row["parent"] is None or row["parent"] in created:
                    created[key] = ComplianceRequirement.objects.create(
                        version=version,
                        parent=created.get(row["parent"]),
                        code=key,
                        title=row["title"],
                        category=row["category"],
                        requirement_type=row["type"],
                        obligation=row["obligation"],
                        applicability=row["applicability"],
                        source=row["source"],
                    )
                    del pending[key]
        framework.active_version = version
        framework.is_active = True
        framework.revision += 1
        framework.save(
            update_fields=["active_version", "is_active", "revision", "updated_at"]
        )
        event(user, framework, "framework_published")
        return version

    @classmethod
    @transaction.atomic
    def activate(cls, user, framework, *, revision, version_uuid=None):
        framework = cls.lock_framework(user, framework, revision)
        version = (
            framework.versions.filter(uuid=version_uuid, is_deleted=False).first()
            if version_uuid
            else None
        )
        if version_uuid and not version:
            raise ValidationError("Version does not belong to this framework.")
        framework.active_version = version
        framework.is_active = bool(version)
        framework.revision += 1
        framework.save(
            update_fields=["active_version", "is_active", "revision", "updated_at"]
        )
        event(
            user, framework, "framework_activated" if version else "framework_retired"
        )
        return framework

    @classmethod
    @transaction.atomic
    def create_control(cls, user, *, code, name, definition, department=None):
        WorkflowDefinitionService.require_admin(user, department)
        Company.objects.select_for_update().get(pk=user.company_id)
        code = schema.code(code)
        definition = schema.control(definition)
        WorkflowDefinitionService.validate_assignment(
            user.company_id, definition["owner"]
        )
        if ComplianceControl.objects.filter(
            company_id=user.company_id, code=code
        ).exists():
            raise ValidationError("Control code already exists.")
        control = ComplianceControl.objects.create(
            company=user.company,
            department_id=department,
            code=code,
            name=schema.text(name),
            definition=definition,
        )
        event(user, control, "control_created")
        return control

    @classmethod
    @transaction.atomic
    def update_control(
        cls, user, control, *, revision, name, definition, is_active=True
    ):
        control = ComplianceControl.objects.select_for_update().get(pk=control.pk)
        if not cls.controls(user).filter(pk=control.pk).exists():
            raise PermissionDenied("Control unavailable.")
        WorkflowDefinitionService.require_admin(user, control.department_id)
        if revision != control.revision:
            raise WorkflowConflict()
        definition = schema.control(definition)
        WorkflowDefinitionService.validate_assignment(
            user.company_id, definition["owner"]
        )
        if type(is_active) is not bool:
            raise ValidationError("Active must be a boolean.")
        control.name, control.definition, control.is_active = (
            schema.text(name),
            definition,
            is_active,
        )
        control.revision += 1
        control.save(
            update_fields=["name", "definition", "is_active", "revision", "updated_at"]
        )
        event(user, control, "control_updated")
        return control

    @classmethod
    @transaction.atomic
    def map_control(cls, user, control, *, revision, requirement_uuid, is_active=True):
        control = ComplianceControl.objects.select_for_update().get(pk=control.pk)
        if not cls.controls(user).filter(pk=control.pk).exists():
            raise PermissionDenied("Control unavailable.")
        WorkflowDefinitionService.require_admin(user, control.department_id)
        if revision != control.revision:
            raise WorkflowConflict()
        req = ComplianceRequirement.objects.filter(
            uuid=requirement_uuid,
            version__framework__in=cls.frameworks(user),
            is_deleted=False,
        ).first()
        if not req or type(is_active) is not bool:
            raise ValidationError("Requirement mapping is unavailable.")
        mapping, _ = ComplianceControlMapping.objects.update_or_create(
            company=user.company,
            requirement=req,
            control=control,
            defaults={"is_active": is_active},
        )
        control.revision += 1
        control.save(update_fields=["revision", "updated_at"])
        event(user, control, "control_mapped")
        return mapping
