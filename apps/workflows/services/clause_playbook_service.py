from datetime import date

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.documents.services import DocumentPermissionService
from apps.workflows.models import ClausePlaybook, ClausePlaybookVersion, ClauseType
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services import clause_schema as validation
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import WorkflowConflict
from apps.workflows.services.workflow_events import workflow_event


class ClausePlaybookUnavailable(ValidationError):
    pass


class ClausePlaybookService:
    @staticmethod
    def visible(user):
        if not WorkflowPermissionService.active(user):
            return ClausePlaybook.objects.none()
        qs = ClausePlaybook.objects.filter(
            company_id=user.company_id, is_deleted=False
        ).select_related("department", "active_version")
        if DocumentPermissionService.is_company_admin(user):
            return qs
        return qs.filter(
            Q(department__isnull=True)
            | Q(
                department_id__in=DocumentPermissionService.get_user_department_ids(
                    user
                )
            )
        )

    @staticmethod
    def require(user, playbook, manage=False):
        if (
            not WorkflowPermissionService.active(user)
            or playbook.company_id != user.company_id
            or playbook.is_deleted
            or not WorkflowPermissionService.department_allowed(
                user, playbook.department_id, manage=manage
            )
        ):
            raise PermissionDenied("Legal playbook access is not permitted.")

    @staticmethod
    def audit(book, actor, event, **metadata):
        workflow_event(
            None,
            actor,
            event,
            definition=book,
            playbook_uuid=str(book.uuid),
            **metadata,
        )

    @classmethod
    @transaction.atomic
    def create(cls, user, *, name, description="", department=None, draft=None):
        WorkflowDefinitionService.require_admin(user, department)
        validation.bounded_json(draft or {}, 65536)
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 150
            or not isinstance(description, str)
            or len(description) > 2000
            or draft is not None
            and not isinstance(draft, dict)
        ):
            raise ValidationError("Invalid legal playbook draft properties.")
        book = ClausePlaybook.objects.create(
            company_id=user.company_id,
            department_id=department,
            name=name,
            description=description,
            draft=draft or {},
        )
        cls.audit(book, user, "clause_playbook_created")
        return book

    @classmethod
    @transaction.atomic
    def edit(cls, user, book, expected_revision, **attrs):
        book = ClausePlaybook.objects.select_for_update().get(pk=book.pk)
        cls.require(user, book, manage=True)
        if book.revision != expected_revision:
            raise WorkflowConflict()
        if set(attrs) - {"name", "description", "draft"}:
            raise ValidationError("Only legal playbook draft properties can be edited.")
        validation.bounded_json(attrs, 65536)
        for key, value in attrs.items():
            setattr(book, key, value)
        book.revision += 1
        book.save()
        cls.audit(book, user, "clause_playbook_draft_changed", revision=book.revision)
        return book

    @classmethod
    @transaction.atomic
    def publish(cls, user, book, expected_revision):
        book = ClausePlaybook.objects.select_for_update().get(pk=book.pk)
        cls.require(user, book, manage=True)
        if book.revision != expected_revision:
            raise WorkflowConflict()
        draft = book.draft
        if not isinstance(draft, dict) or set(draft) - {
            "rules",
            "document_types",
            "effective_from",
            "effective_until",
        }:
            raise ValidationError("Invalid legal playbook draft schema.")
        if (
            not isinstance(draft.get("rules"), list)
            or not 1 <= len(draft["rules"]) <= 64
        ):
            raise ValidationError("A playbook requires a bounded rule list.")
        refs = [r.get("clause_type") for r in draft["rules"] if isinstance(r, dict)]
        if any(not isinstance(r, str) for r in refs):
            raise ValidationError("Clause rules require taxonomy UUIDs.")
        # Query UUIDs only after syntactic validation, never expose Django UUID errors.
        from uuid import UUID

        try:
            for ref in refs:
                UUID(ref)
        except (ValueError, TypeError):
            raise ValidationError("Invalid clause taxonomy reference.")
        types = list(
            ClauseType.objects.filter(
                Q(company_id=user.company_id) | Q(company__isnull=True),
                is_active=True,
                is_deleted=False,
            ).order_by("uuid")[:129]
        )
        if len(types) > 128:
            raise ValidationError(
                "Published taxonomy exceeds the supported analysis scope."
            )
        taxonomy = {
            str(t.uuid): {
                "code": t.code,
                "name": t.name,
                "category": t.category,
                "aliases": t.aliases,
                "document_types": t.document_types,
                "fact_schema": validation.schema(t.fact_schema),
            }
            for t in types
        }
        normalized = validation.rules(draft.get("rules"), taxonomy)
        validation.bounded_json(taxonomy, 65536)
        document_types = validation.strings(draft.get("document_types", []), 32, 80)
        effective_from = draft.get("effective_from", timezone.localdate().isoformat())
        effective_until = draft.get("effective_until")
        effective_from = validation.scalar(effective_from, {"type": "date"})
        if effective_from is None:
            raise ValidationError("A playbook requires an effective date.")
        if effective_until is not None:
            effective_until = validation.scalar(effective_until, {"type": "date"})
            if effective_until <= effective_from:
                raise ValidationError("Playbook effective dates are inconsistent.")
        version = ClausePlaybookVersion.objects.create(
            playbook=book,
            number=(book.versions.first().number + 1) if book.versions.exists() else 1,
            rules=normalized,
            taxonomy=taxonomy,
            document_types=document_types,
            effective_from=date.fromisoformat(effective_from),
            effective_until=date.fromisoformat(effective_until)
            if effective_until
            else None,
            published_by=user,
        )
        book.revision += 1
        book.save(update_fields=["revision", "updated_at"])
        cls.audit(
            book,
            user,
            "clause_playbook_published",
            version_uuid=str(version.uuid),
            revision=book.revision,
        )
        return version

    @classmethod
    @transaction.atomic
    def activate(cls, user, book, expected_revision, version=None, active=True):
        # Serialize selection changes throughout one company as well as one playbook.
        from apps.tenants.models import Company

        Company.objects.select_for_update().get(pk=user.company_id)
        book = ClausePlaybook.objects.select_for_update().get(pk=book.pk)
        cls.require(user, book, manage=True)
        if book.revision != expected_revision:
            raise WorkflowConflict()
        if active:
            if not version or version.playbook_id != book.pk or version.is_deleted:
                raise ValidationError(
                    "Activation requires a published version of this playbook."
                )
            candidates = list(
                cls.visible(user)
                .filter(department_id=book.department_id, is_active=True)
                .exclude(pk=book.pk)[:65]
            )
            if len(candidates) > 64:
                raise ValidationError("Playbook selection exceeds the supported scope.")
            for other in candidates:
                old = other.active_version
                if (
                    old
                    and (
                        not old.document_types
                        or not version.document_types
                        or set(old.document_types) & set(version.document_types)
                    )
                    and (
                        not old.effective_until
                        or version.effective_from < old.effective_until
                    )
                    and (
                        not version.effective_until
                        or old.effective_from < version.effective_until
                    )
                ):
                    raise ValidationError(
                        "Overlapping active playbooks require an unambiguous selection policy."
                    )
            book.active_version = version
        book.is_active = active
        book.revision += 1
        book.save(
            update_fields=["active_version", "is_active", "revision", "updated_at"]
        )
        cls.audit(
            book,
            user,
            "clause_playbook_activated" if active else "clause_playbook_deactivated",
            revision=book.revision,
            version_uuid=str(book.active_version.uuid) if book.active_version else None,
        )
        return book

    @classmethod
    def select(cls, user, *, department, document_type, explicit=None, on_date=None):
        on_date = on_date or timezone.localdate()
        if not WorkflowPermissionService.department_allowed(user, department):
            raise PermissionDenied("Legal analysis department is not authorized.")
        qs = cls.visible(user).filter(is_active=True)
        if explicit:
            version = (
                ClausePlaybookVersion.objects.select_related("playbook")
                .filter(uuid=explicit, playbook__in=qs, is_deleted=False)
                .first()
            )
            if (
                not version
                or version.playbook.department_id not in (None, department)
                or version.document_types
                and document_type not in version.document_types
            ):
                raise PermissionDenied(
                    "The selected legal playbook is unavailable in this context."
                )
            cls.require(user, version.playbook)
            return version
        books = list(
            qs.filter(Q(department_id=department) | Q(department__isnull=True))[:65]
        )
        if len(books) > 64:
            raise ValidationError("Playbook selection exceeds the supported scope.")
        versions = [
            b.active_version
            for b in books
            if b.active_version
            and b.active_version.effective_from <= on_date
            and (
                not b.active_version.effective_until
                or on_date < b.active_version.effective_until
            )
            and (
                not b.active_version.document_types
                or document_type in b.active_version.document_types
            )
        ]
        departmental = [
            v
            for v in versions
            if v.playbook.department_id == department and department is not None
        ]
        candidates = departmental or versions
        if len(candidates) != 1:
            raise ClausePlaybookUnavailable(
                "Legal playbook selection is unavailable or ambiguous."
            )
        return candidates[0]
