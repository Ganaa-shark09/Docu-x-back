from rest_framework.permissions import BasePermission

from apps.accounts.models import User


class IsLLMProviderAdmin(BasePermission):
    """
    LLM infrastructure configuration is security-sensitive.

    Only platform administrators and company administrators may
    create, update, delete, or inspect provider configuration.
    """

    message = "Only company administrators may manage LLM providers."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)

        if not user or not getattr(user, "is_authenticated", False):
            return False

        if getattr(user, "is_superuser", False):
            return True

        return getattr(user, "role", None) in {
            User.Role.PLATFORM_ADMIN,
            User.Role.COMPANY_ADMIN,
        }
