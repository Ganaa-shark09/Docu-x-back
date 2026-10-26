from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from apps.audit.models import AuditLog
from apps.audit.services import AuditLogService
from apps.documents.models import Document, SensitivityPolicy


class GovernanceConflict(APIException):
    status_code = 409
    default_detail = "Governance changed; reload before updating."
    default_code = "governance_conflict"


class DocumentGovernanceService:
    FIELDS = (
        "owner_user",
        "custodian",
        "department",
        "sensitivity_label",
        "retention_policy",
        "retention_start",
        "legal_hold",
    )
    CLASSES = set(Document.Sensitivity.values)

    @classmethod
    def sensitivity(cls, document):
        policy = SensitivityPolicy.objects.filter(
            company_id=document.company_id,
            code=document.sensitivity_label,
            is_deleted=False,
        ).first()
        security_class = policy.security_class if policy else document.sensitivity_label
        # An inactive or unknown label must never fall back to a permissive class.
        valid = security_class in cls.CLASSES and (policy is None or policy.is_active)
        return {
            "security_class": security_class if valid else "restricted",
            "allow_ai": valid and (policy.allow_ai if policy else True),
            "allow_export": valid and (policy.allow_export if policy else True),
            "allow_cloud": valid
            and security_class not in {"restricted", "highly_confidential"}
            and (policy.allow_cloud if policy else True),
        }

    @classmethod
    def validate_label(cls, company, value):
        if value in cls.CLASSES:
            policy = SensitivityPolicy.objects.filter(
                company=company, code=value, is_deleted=False
            ).first()
            if policy and not policy.is_active:
                raise ValidationError(
                    {"sensitivity_label": "Sensitivity policy is inactive."}
                )
            return value
        if not SensitivityPolicy.objects.filter(
            company=company, code=value, is_active=True, is_deleted=False
        ).exists():
            raise ValidationError(
                {"sensitivity_label": "Unknown company sensitivity policy."}
            )
        return value

    @staticmethod
    def expired(document):
        return bool(
            document.retention_expires_at
            and document.retention_expires_at <= timezone.now()
        )

    @classmethod
    def state(cls, document):
        return (
            "legal_hold"
            if document.legal_hold
            else "expired"
            if cls.expired(document)
            else "active"
        )

    @classmethod
    def deletion_eligible(cls, document):
        return cls.expired(document) and not document.legal_hold

    @staticmethod
    def snapshot(document):
        return {
            key: str(getattr(document, key + "_id", None))
            if key in {"owner_user", "custodian", "department", "retention_policy"}
            else str(getattr(document, key))
            for key in DocumentGovernanceService.FIELDS
        }

    @classmethod
    @transaction.atomic
    def update(cls, user, document, values, expected_revision=None, request=None):
        from apps.documents.services.document_permission_service import (
            DocumentPermissionService,
        )

        current = Document.objects.select_for_update().get(
            pk=document.pk, company=user.company, is_deleted=False
        )
        DocumentPermissionService.require_action(user, current, "administer")
        if (
            expected_revision is not None
            and expected_revision != current.governance_revision
        ):
            raise GovernanceConflict()
        before = cls.snapshot(current)
        # Retention shortening, holds and classification are organization-level
        # controls: document ownership alone cannot override them.
        organization_fields = {
            "retention_policy",
            "retention_start",
            "legal_hold",
            "sensitivity_label",
        }
        changed = {
            key
            for key, value in values.items()
            if key in cls.FIELDS and getattr(current, key) != value
        }
        if (
            changed & organization_fields
            and not DocumentPermissionService.is_company_admin(user)
        ):
            raise PermissionDenied(
                "Company administrator authority is required for classification and retention changes."
            )
        for key, value in values.items():
            if key not in cls.FIELDS:
                raise ValidationError({key: "Unsupported governance field."})
            if value is not None and key in {
                "owner_user",
                "custodian",
                "department",
                "retention_policy",
            }:
                if (
                    value.company_id != user.company_id
                    or getattr(value, "is_deleted", False)
                    or not getattr(value, "is_active", True)
                ):
                    raise ValidationError(
                        {key: "Invalid company governance reference."}
                    )
            if key == "sensitivity_label":
                cls.validate_label(user.company, value)
            setattr(current, key, value)
        if "retention_policy" in values:
            current.retention_duration_days = (
                current.retention_policy.duration_days
                if current.retention_policy
                else user.company.data_retention_days
            )
        if {"retention_policy", "retention_start"} & values.keys():
            if current.retention_start is None:
                raise ValidationError(
                    {"retention_start": "A retention start is required."}
                )
            if current.retention_start > timezone.now():
                raise ValidationError(
                    {"retention_start": "Retention start cannot be in the future."}
                )
            current.retention_expires_at = current.retention_start + timedelta(
                days=current.retention_duration_days
            )
        current.governance_revision += 1
        current.save()
        AuditLogService.log(
            company=user.company,
            user=user,
            event_type=AuditLog.EventType.DOCUMENT_GOVERNANCE_CHANGED,
            action="Document governance updated.",
            resource_type="document",
            resource_uuid=current.uuid,
            ip_address=request.META.get("REMOTE_ADDR") if request else None,
            user_agent=request.META.get("HTTP_USER_AGENT", "") if request else "",
            metadata={
                "before": before,
                "after": cls.snapshot(current),
                "revision": current.governance_revision,
            },
        )
        return current

    @classmethod
    @transaction.atomic
    def delete(cls, user, document, request=None):
        from apps.documents.services.document_permission_service import (
            DocumentPermissionService,
        )

        current = Document.objects.select_for_update().get(
            pk=document.pk, company=user.company, is_deleted=False
        )
        DocumentPermissionService.require_action(user, current, "delete")
        current.soft_delete()
        AuditLogService.log(
            company=user.company,
            user=user,
            event_type=AuditLog.EventType.DOCUMENT_GOVERNANCE_CHANGED,
            action="Document soft deleted.",
            resource_type="document",
            resource_uuid=current.uuid,
            metadata={"operation": "delete", "revision": current.governance_revision},
        )
