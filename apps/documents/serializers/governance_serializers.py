from rest_framework import serializers

from apps.accounts.models import User
from apps.documents.models import (
    Document,
    DocumentFieldPolicy,
    RetentionPolicy,
    SensitivityPolicy,
)
from apps.documents.services.document_governance_service import (
    DocumentGovernanceService,
)
from apps.workspaces.models import Department


class TenantReferenceField(serializers.SlugRelatedField):
    def get_queryset(self):
        request = self.context["request"]
        queryset = super().get_queryset().filter(company_id=request.user.company_id)
        if hasattr(queryset.model, "is_deleted"):
            queryset = queryset.filter(is_deleted=False)
        if hasattr(queryset.model, "is_active"):
            queryset = queryset.filter(is_active=True)
        return queryset


class DocumentGovernanceSerializer(serializers.ModelSerializer):
    owner_user = TenantReferenceField(
        slug_field="id", queryset=User.objects.all(), required=False, allow_null=True
    )
    custodian = TenantReferenceField(
        slug_field="id", queryset=User.objects.all(), required=False, allow_null=True
    )
    department = TenantReferenceField(
        slug_field="id",
        queryset=Department.objects.all(),
        required=False,
        allow_null=True,
    )
    retention_policy = TenantReferenceField(
        slug_field="uuid",
        queryset=RetentionPolicy.objects.all(),
        required=False,
        allow_null=True,
    )
    expected_revision = serializers.IntegerField(
        write_only=True, min_value=1, required=True
    )
    retention_status = serializers.SerializerMethodField()
    deletion_eligible = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "owner_user",
            "custodian",
            "department",
            "sensitivity_label",
            "retention_policy",
            "retention_duration_days",
            "retention_start",
            "retention_expires_at",
            "legal_hold",
            "governance_revision",
            "expected_revision",
            "retention_status",
            "deletion_eligible",
        ]
        read_only_fields = [
            "retention_duration_days",
            "retention_expires_at",
            "governance_revision",
        ]

    def get_retention_status(self, obj):
        return DocumentGovernanceService.state(obj)

    def get_deletion_eligible(self, obj):
        return DocumentGovernanceService.deletion_eligible(obj)

    def update(self, instance, validated_data):
        request = self.context["request"]
        revision = validated_data.pop("expected_revision")
        return DocumentGovernanceService.update(
            request.user,
            instance,
            validated_data,
            expected_revision=revision,
            request=request,
        )


class SensitivityPolicySerializer(serializers.ModelSerializer):
    security_class = serializers.ChoiceField(choices=Document.Sensitivity.choices)
    minimum_access = serializers.ChoiceField(
        choices=["view", "comment", "edit", "review", "approve", "admin"]
    )

    class Meta:
        model = SensitivityPolicy
        fields = [
            "uuid",
            "code",
            "name",
            "security_class",
            "minimum_access",
            "allow_ai",
            "allow_export",
            "allow_cloud",
            "is_active",
        ]
        read_only_fields = ["uuid"]
        validators = []

    def validate(self, attrs):
        code = attrs.get("code", getattr(self.instance, "code", None))
        security_class = attrs.get(
            "security_class", getattr(self.instance, "security_class", "internal")
        )
        ranks = {
            "public": 0,
            "internal": 1,
            "confidential": 2,
            "highly_confidential": 3,
            "restricted": 4,
        }
        if code in ranks and ranks[security_class] < ranks[code]:
            raise serializers.ValidationError(
                "A built-in sensitivity code cannot be downgraded."
            )
        if self.instance and code != self.instance.code:
            raise serializers.ValidationError({"code": "Policy codes are immutable."})
        duplicate = SensitivityPolicy.objects.filter(
            company=self.context["request"].user.company, code=code
        )
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError(
                {"code": "Code already exists for this company."}
            )
        return attrs


class RetentionPolicySerializer(serializers.ModelSerializer):
    duration_days = serializers.IntegerField(min_value=1, max_value=365000)

    class Meta:
        model = RetentionPolicy
        fields = ["uuid", "name", "duration_days", "is_active"]
        read_only_fields = ["uuid"]


class DocumentFieldPolicySerializer(serializers.ModelSerializer):
    user = TenantReferenceField(
        slug_field="id", queryset=User.objects.all(), required=False, allow_null=True
    )
    role = serializers.ChoiceField(
        choices=[""] + list(User.Role.values), required=False
    )

    class Meta:
        model = DocumentFieldPolicy
        fields = [
            "uuid",
            "field_key",
            "user",
            "role",
            "can_view",
            "can_use_ai",
            "can_export",
            "masked_view",
        ]
        read_only_fields = ["uuid"]

    def validate(self, attrs):
        if attrs.get("user") and attrs.get("role"):
            raise serializers.ValidationError("Specify at most one principal.")
        return attrs
