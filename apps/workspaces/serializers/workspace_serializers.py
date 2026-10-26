from rest_framework import serializers

from apps.workspaces.models import Department, DepartmentMembership
from apps.workspaces.permissions.workspace_permissions import IsCompanyWorkspaceUser
from apps.workspaces.permissions.workspace_permissions import workspace_company


class DepartmentSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)

    class Meta:
        model = Department
        fields = [
            "id",
            "uuid",
            "company_name",
            "name",
            "slug",
            "department_type",
            "description",
            "is_active",
            "default_sensitivity_label",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "uuid",
            "company_name",
            "slug",
            "created_at",
            "updated_at",
        ]

    def create(self, validated_data):
        request = self.context["request"]
        return Department.objects.create(
            company=workspace_company(request),
            **validated_data,
        )


class DepartmentMembershipSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = DepartmentMembership
        fields = [
            "uuid",
            "department",
            "department_name",
            "user",
            "user_email",
            "username",
            "role",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "uuid",
            "department_name",
            "user_email",
            "username",
            "created_at",
            "updated_at",
        ]

    def validate_department(self, department):
        request = self.context["request"]

        if department.company_id != workspace_company(request).pk:
            raise serializers.ValidationError(
                "Department does not belong to your company."
            )

        if not request.user.is_superuser and not IsCompanyWorkspaceUser.can_manage(
            request.user, department
        ):
            raise serializers.ValidationError(
                "Department administrator authority is required."
            )

        return department

    def validate_user(self, user):
        request = self.context["request"]

        if user.company_id != workspace_company(request).pk:
            raise serializers.ValidationError("User does not belong to your company.")

        return user

    def create(self, validated_data):
        request = self.context["request"]
        return DepartmentMembership.objects.create(
            company=workspace_company(request),
            **validated_data,
        )
