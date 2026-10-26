from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.documents.models import Document, DocumentAccessPolicy, SensitivityPolicy
from apps.workspaces.models import DepartmentMembership


class DocumentPermissionService:
    ACCESS_ORDER = {
        DocumentAccessPolicy.AccessLevel.VIEW: 1,
        DocumentAccessPolicy.AccessLevel.COMMENT: 2,
        DocumentAccessPolicy.AccessLevel.EDIT: 3,
        DocumentAccessPolicy.AccessLevel.REVIEW: 4,
        DocumentAccessPolicy.AccessLevel.APPROVE: 5,
        DocumentAccessPolicy.AccessLevel.ADMIN: 6,
    }

    DEPARTMENT_ROLE_ACCESS = {
        DepartmentMembership.Role.VIEWER: DocumentAccessPolicy.AccessLevel.VIEW,
        DepartmentMembership.Role.MEMBER: DocumentAccessPolicy.AccessLevel.EDIT,
        DepartmentMembership.Role.REVIEWER: DocumentAccessPolicy.AccessLevel.REVIEW,
        DepartmentMembership.Role.DEPARTMENT_ADMIN: DocumentAccessPolicy.AccessLevel.ADMIN,
    }

    @classmethod
    def is_company_admin(cls, user):
        return bool(
            user
            and user.is_authenticated
            and (
                user.is_superuser
                or getattr(user, "role", None) == User.Role.PLATFORM_ADMIN
                or getattr(user, "role", None) == User.Role.COMPANY_ADMIN
            )
        )

    @classmethod
    def get_allowed_access_levels(cls, required_access):
        required_rank = cls.ACCESS_ORDER.get(required_access, 999)

        return [
            access_level
            for access_level, rank in cls.ACCESS_ORDER.items()
            if rank >= required_rank
        ]

    @classmethod
    def get_user_department_ids(
        cls,
        user,
        required_access=DocumentAccessPolicy.AccessLevel.VIEW,
    ):
        if (
            not user
            or not user.is_authenticated
            or not getattr(user, "company_id", None)
        ):
            return []

        required_rank = cls.ACCESS_ORDER.get(required_access, 1)

        allowed_department_roles = [
            role
            for role, access_level in cls.DEPARTMENT_ROLE_ACCESS.items()
            if cls.ACCESS_ORDER[access_level] >= required_rank
        ]

        return list(
            DepartmentMembership.objects.filter(
                company=user.company,
                user=user,
                is_active=True,
                is_deleted=False,
                department__is_deleted=False,
                department__is_active=True,
                role__in=allowed_department_roles,
            ).values_list("department_id", flat=True)
        )

    @classmethod
    def get_accessible_document_queryset(
        cls,
        user,
        required_access=DocumentAccessPolicy.AccessLevel.VIEW,
        require_ai=False,
        scope=None,
        ai_policy_only=False,
    ):
        if (
            not user
            or not user.is_authenticated
            or not user.is_active
            or not getattr(user, "company_id", None)
            or user.company.is_deleted
            or user.company.status != "active"
        ):
            return Document.objects.none()

        base_queryset = Document.objects.filter(
            company=user.company,
            is_deleted=False,
        )

        # Expiration limits access even on legal hold; a hold prevents deletion,
        # not access revocation. Administrators retain metadata access for disposition.
        if not cls.is_company_admin(user):
            base_queryset = base_queryset.filter(
                Q(retention_expires_at__isnull=True)
                | Q(retention_expires_at__gt=timezone.now())
            )

        if scope:
            base_queryset = base_queryset.filter(scope=scope)

        if require_ai or ai_policy_only:
            from apps.documents.services.field_permission_service import (
                FieldPermissionService,
            )

            base_queryset = base_queryset.exclude(status=Document.Status.ARCHIVED)
            if require_ai:
                base_queryset = base_queryset.filter(is_ai_ready=True)
            base_queryset = base_queryset.filter(
                Q(retention_expires_at__isnull=True)
                | Q(retention_expires_at__gt=timezone.now())
            )
            base_queryset = base_queryset.exclude(
                pk__in=FieldPermissionService.denied_document_ids(user, "ai")
            )
            base_queryset = base_queryset.exclude(
                pk__in=FieldPermissionService.denied_document_ids(user, "view")
            )
            denied = (
                DocumentAccessPolicy.objects.filter(
                    document__company=user.company, is_deleted=False, can_use_ai=False
                )
                .filter(Q(user=user) | Q(user__isnull=True, role=user.role))
                .values("document_id")
            )
            base_queryset = base_queryset.exclude(pk__in=denied)
            base_queryset = base_queryset.exclude(
                sensitivity_label__in=SensitivityPolicy.objects.filter(
                    company=user.company, is_deleted=False
                )
                .filter(Q(allow_ai=False) | Q(is_active=False))
                .values("code")
            )
            base_queryset = base_queryset.filter(
                Q(sensitivity_label__in=Document.Sensitivity.values)
                | Q(
                    sensitivity_label__in=SensitivityPolicy.objects.filter(
                        company=user.company,
                        is_deleted=False,
                        is_active=True,
                        security_class__in=Document.Sensitivity.values,
                    ).values("code")
                )
            )

        # Sensitivity policies can require stronger access even for list/retrieval.
        for policy in SensitivityPolicy.objects.filter(
            company=user.company, is_deleted=False
        ).exclude(minimum_access="view"):
            if not cls.is_company_admin(user):
                allowed = cls._access_query(user, policy.minimum_access)
                permitted_ids = Document.objects.filter(allowed).values("pk")
                base_queryset = base_queryset.exclude(
                    Q(sensitivity_label=policy.code) & ~Q(pk__in=permitted_ids)
                )

        # External documents are user-private by default.
        if scope == Document.Scope.EXTERNAL:
            if cls.is_company_admin(user):
                return base_queryset

            return base_queryset.filter(uploaded_by=user)

        # Internal company documents follow department/document policy rules.
        if scope == Document.Scope.INTERNAL and cls.is_company_admin(user):
            return base_queryset

        # If scope is not provided, company admins can see both internal and external.
        if not scope and cls.is_company_admin(user):
            return base_queryset

        allowed_access_levels = cls.get_allowed_access_levels(required_access)
        department_ids = cls.get_user_department_ids(
            user=user,
            required_access=required_access,
        )

        internal_access_query = Q(scope=Document.Scope.INTERNAL) & (
            cls._ownership_query(user)
            | Q(department_id__in=department_ids)
            | Q(
                access_policies__user=user,
                access_policies__access_level__in=allowed_access_levels,
                access_policies__is_deleted=False,
            )
            | Q(
                access_policies__user__isnull=True,
                access_policies__role=user.role,
                access_policies__access_level__in=allowed_access_levels,
                access_policies__is_deleted=False,
            )
        )

        external_access_query = Q(scope=Document.Scope.EXTERNAL, uploaded_by=user)

        if require_ai or ai_policy_only:
            explicit_ai_query = Q(
                access_policies__user=user,
                access_policies__access_level__in=allowed_access_levels,
                access_policies__can_use_ai=True,
                access_policies__is_deleted=False,
            ) | Q(
                access_policies__user__isnull=True,
                access_policies__role=user.role,
                access_policies__access_level__in=allowed_access_levels,
                access_policies__can_use_ai=True,
                access_policies__is_deleted=False,
            )

            internal_access_query = Q(scope=Document.Scope.INTERNAL) & (
                cls._ownership_query(user)
                | Q(department_id__in=department_ids)
                | explicit_ai_query
            )

        return base_queryset.filter(
            internal_access_query | external_access_query
        ).distinct()

    @staticmethod
    def _ownership_query(user):
        # Preserve legacy uploader authority only while no explicit owner exists.
        return Q(owner_user=user) | Q(owner_user__isnull=True, uploaded_by=user)

    @classmethod
    def _access_query(cls, user, level):
        levels = cls.get_allowed_access_levels(level)
        return (
            cls._ownership_query(user)
            | Q(department_id__in=cls.get_user_department_ids(user, level))
            | Q(
                access_policies__is_deleted=False,
                access_policies__access_level__in=levels,
                access_policies__user=user,
            )
            | Q(
                access_policies__is_deleted=False,
                access_policies__access_level__in=levels,
                access_policies__user__isnull=True,
                access_policies__role=user.role,
            )
        )

    @classmethod
    def can_action(cls, user, document, action):
        from apps.documents.services.document_governance_service import (
            DocumentGovernanceService,
        )
        from apps.documents.services.field_permission_service import (
            FieldPermissionService,
        )

        levels = {
            "governance_read": "view",
            "view": "view",
            "download": "view",
            "edit": "edit",
            "delete": "admin",
            "ai": "view",
            "extract": "review",
            "correct": "review",
            "export": "view",
            "workflow": "review",
            "administer": "admin",
        }
        if (
            action not in levels
            or not document
            or document.company_id != getattr(user, "company_id", None)
        ):
            return False
        if not cls.has_document_access(
            user,
            document,
            levels[action],
            require_ai=action in {"ai", "extract", "workflow"},
        ):
            return False
        if action == "governance_read":
            return cls.is_company_admin(user) or not DocumentGovernanceService.expired(
                document
            )
        if action == "view" and DocumentGovernanceService.expired(document):
            return False
        if action == "delete" and (
            document.legal_hold
            or not DocumentGovernanceService.deletion_eligible(document)
        ):
            return False
        if action in {
            "edit",
            "correct",
            "extract",
            "workflow",
            "export",
            "download",
            "ai",
        } and (
            DocumentGovernanceService.expired(document)
            or document.status == Document.Status.ARCHIVED
        ):
            return False
        rules = DocumentAccessPolicy.objects.filter(
            document=document, is_deleted=False
        ).filter(Q(user=user) | Q(user__isnull=True, role=user.role))
        if action in {"download", "export", "ai", "extract"}:
            flag = {
                "download": "can_download",
                "export": "can_export",
                "ai": "can_use_ai",
                "extract": "can_use_ai",
            }[action]
            if rules.filter(**{flag: False}).exists():
                return False
        if action in {"ai", "extract", "export"}:
            policy = DocumentGovernanceService.sensitivity(document)
            if not policy["allow_export" if action == "export" else "allow_ai"]:
                return False
        if action == "export":
            if FieldPermissionService.has_restrictions(user, document, "export"):
                return False
            if not (
                cls.is_company_admin(user)
                or document.owner_user_id == user.pk
                or (
                    document.owner_user_id is None
                    and document.uploaded_by_id == user.pk
                )
                or rules.filter(can_export=True).exists()
            ):
                return False
        if action == "download" and FieldPermissionService.has_restrictions(
            user, document, "view"
        ):
            return False
        return True

    @classmethod
    def require_action(cls, user, document, action):
        if not cls.can_action(user, document, action):
            raise PermissionDenied(f"Document action '{action}' is not permitted.")

    @classmethod
    def has_document_access(
        cls,
        user,
        document,
        required_access=DocumentAccessPolicy.AccessLevel.VIEW,
        require_ai=False,
    ):
        if not document:
            return False

        return (
            cls.get_accessible_document_queryset(
                user=user,
                required_access=required_access,
                require_ai=require_ai,
                scope=document.scope,
            )
            .filter(pk=document.pk)
            .exists()
        )

    @classmethod
    def can_download_document(cls, user, document, assume_view_access=False):
        return cls.can_action(user, document, "download")

    @classmethod
    def get_highest_access_level(cls, user, document):
        if not cls.has_document_access(user, document):
            return None
        if cls.is_company_admin(user):
            return DocumentAccessPolicy.AccessLevel.ADMIN

        if not user or not user.is_authenticated or not document:
            return None

        if document.scope == Document.Scope.EXTERNAL:
            if document.uploaded_by_id == user.id:
                return DocumentAccessPolicy.AccessLevel.ADMIN
            return None

        if document.owner_user_id == user.id or (
            document.owner_user_id is None and document.uploaded_by_id == user.id
        ):
            return DocumentAccessPolicy.AccessLevel.ADMIN

        highest_rank = 0
        highest_access = None

        department_membership = DepartmentMembership.objects.filter(
            company=user.company,
            department=document.department,
            user=user,
            is_active=True,
            is_deleted=False,
        ).first()

        if department_membership:
            access_level = cls.DEPARTMENT_ROLE_ACCESS.get(department_membership.role)
            rank = cls.ACCESS_ORDER.get(access_level, 0)

            if rank > highest_rank:
                highest_rank = rank
                highest_access = access_level

        policies = DocumentAccessPolicy.objects.filter(
            document=document,
            is_deleted=False,
        ).filter(Q(user=user) | Q(user__isnull=True, role=user.role))

        for policy in policies:
            rank = cls.ACCESS_ORDER.get(policy.access_level, 0)

            if rank > highest_rank:
                highest_rank = rank
                highest_access = policy.access_level

        return highest_access
