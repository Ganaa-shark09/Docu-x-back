from django.db.models import Q

from apps.accounts.models import User
from apps.documents.models import Document, DocumentAccessPolicy
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
        required_rank = cls.ACCESS_ORDER.get(required_access, 1)

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
    ):
        if (
            not user
            or not user.is_authenticated
            or not getattr(user, "company_id", None)
        ):
            return Document.objects.none()

        base_queryset = Document.objects.filter(
            company=user.company,
            is_deleted=False,
        )

        if scope:
            base_queryset = base_queryset.filter(scope=scope)

        if require_ai:
            base_queryset = base_queryset.filter(is_ai_ready=True)

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
            Q(uploaded_by=user)
            | Q(department_id__in=department_ids)
            | Q(
                access_policies__user=user,
                access_policies__access_level__in=allowed_access_levels,
                access_policies__is_deleted=False,
            )
            | Q(
                access_policies__role=user.role,
                access_policies__access_level__in=allowed_access_levels,
                access_policies__is_deleted=False,
            )
        )

        external_access_query = Q(scope=Document.Scope.EXTERNAL, uploaded_by=user)

        if require_ai:
            explicit_ai_query = Q(
                access_policies__user=user,
                access_policies__access_level__in=allowed_access_levels,
                access_policies__can_use_ai=True,
                access_policies__is_deleted=False,
            ) | Q(
                access_policies__role=user.role,
                access_policies__access_level__in=allowed_access_levels,
                access_policies__can_use_ai=True,
                access_policies__is_deleted=False,
            )

            internal_access_query = Q(scope=Document.Scope.INTERNAL) & (
                Q(uploaded_by=user)
                | Q(department_id__in=department_ids)
                | explicit_ai_query
            )

        return base_queryset.filter(
            internal_access_query | external_access_query
        ).distinct()

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
        if not document:
            return False

        if not assume_view_access and not cls.has_document_access(
            user=user,
            document=document,
            required_access=DocumentAccessPolicy.AccessLevel.VIEW,
        ):
            return False

        if cls.is_company_admin(user) or document.uploaded_by_id == getattr(
            user, "id", None
        ):
            return True

        prefetched = getattr(document, "_prefetched_objects_cache", {}).get(
            "access_policies"
        )

        if prefetched is None:
            policies = list(
                DocumentAccessPolicy.objects.filter(
                    document=document,
                    is_deleted=False,
                ).filter(Q(user=user) | Q(role=getattr(user, "role", "")))
            )
        else:
            policies = [
                policy
                for policy in prefetched
                if not policy.is_deleted
                and (
                    policy.user_id == getattr(user, "id", None)
                    or (policy.role and policy.role == getattr(user, "role", ""))
                )
            ]

        # Explicit document policy controls download when one applies. Department-
        # based access falls back to view access when there is no explicit policy.
        if policies:
            return any(policy.can_download for policy in policies)

        return True

    @classmethod
    def get_highest_access_level(cls, user, document):
        if cls.is_company_admin(user):
            return DocumentAccessPolicy.AccessLevel.ADMIN

        if not user or not user.is_authenticated or not document:
            return None

        if document.scope == Document.Scope.EXTERNAL:
            if document.uploaded_by_id == user.id:
                return DocumentAccessPolicy.AccessLevel.ADMIN
            return None

        if document.uploaded_by_id == user.id:
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
        ).filter(Q(user=user) | Q(role=user.role))

        for policy in policies:
            rank = cls.ACCESS_ORDER.get(policy.access_level, 0)

            if rank > highest_rank:
                highest_rank = rank
                highest_access = policy.access_level

        return highest_access
