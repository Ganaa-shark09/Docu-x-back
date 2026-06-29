from rest_framework.permissions import BasePermission


class IsCompanyDocumentUser(BasePermission):
    message = "You do not have permission to access this document."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "company_id", None)
        )

    def has_object_permission(self, request, view, obj):
        return obj.company_id == request.user.company_id
