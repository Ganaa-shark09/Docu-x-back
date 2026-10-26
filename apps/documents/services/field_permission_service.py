from django.db.models import Count, Q

from apps.documents.models import DocumentFieldPolicy


class FieldPermissionService:
    MASK = "[REDACTED]"

    @staticmethod
    def principal(user):
        return (
            Q(user=user)
            | Q(user__isnull=True, role=user.role)
            | Q(user__isnull=True, role="")
        )

    @classmethod
    def denied_document_ids(cls, user, action):
        flag = {"view": "can_view", "ai": "can_use_ai", "export": "can_export"}[action]
        return (
            DocumentFieldPolicy.objects.filter(
                is_deleted=False, document__company_id=user.company_id
            )
            .values("document_id", "field_key")
            .annotate(
                allowed=Count("pk", filter=cls.principal(user) & Q(**{flag: True})),
                denied=Count("pk", filter=cls.principal(user) & Q(**{flag: False})),
            )
            .filter(Q(allowed=0) | Q(denied__gt=0))
            .values("document_id")
        )

    @classmethod
    def rules(cls, user, document):
        if document.company_id != user.company_id:
            return {}
        cache = getattr(document, "_prefetched_objects_cache", {}).get("field_policies")
        policies = (
            cache
            if cache is not None
            else document.field_policies.filter(is_deleted=False)
        )
        grouped = {}
        for rule in policies:
            if not rule.is_deleted:
                grouped.setdefault(rule.field_key, []).append(rule)
        return grouped

    @staticmethod
    def permits(user, rules, action):
        flag = {"view": "can_view", "ai": "can_use_ai", "export": "can_export"}[action]
        matched = [
            p
            for p in rules
            if p.user_id == user.pk or (p.user_id is None and p.role in {"", user.role})
        ]
        return bool(matched) and all(getattr(p, flag) for p in matched)

    @classmethod
    def has_restrictions(cls, user, document, action):
        return any(
            not cls.permits(user, rules, action)
            for rules in cls.rules(user, document).values()
        )

    @classmethod
    def field_allowed(cls, user, document, key, action="view"):
        rules = cls.rules(user, document).get(key)
        return rules is None or cls.permits(user, rules, action)

    @classmethod
    def protect_field(cls, user, document, key, data):
        if cls.field_allowed(user, document, key):
            return data
        # Values, normalization, evidence and correction metadata can all carry
        # the protected value. Preserve only the public field identifier/label.
        result = {"field_key": key, "label": data.get("label", key), "restricted": True}
        rules = cls.rules(user, document).get(key, [])
        matching = [
            p
            for p in rules
            if p.user_id == user.pk or (p.user_id is None and p.role in {"", user.role})
        ]
        if matching and all(p.masked_view for p in matching):
            result["value"] = cls.MASK
        return result
