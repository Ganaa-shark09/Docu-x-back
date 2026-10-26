from rest_framework import serializers


class PolicyCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    code = serializers.SlugField(max_length=80)
    category_uuid = serializers.UUIDField()
    department = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    draft = serializers.JSONField(required=False)


class PolicyDraftSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=1)
    workflow_revision = serializers.IntegerField(min_value=1, required=False)
    draft = serializers.JSONField()


class PolicySubmitSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=1)
    reviewer = serializers.JSONField()
    approver = serializers.JSONField()
    idempotency_key = serializers.CharField(max_length=128)


class PolicyQuestionSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=8000)
    context = serializers.JSONField(required=False, default=dict)
    conversation_uuid = serializers.UUIDField(required=False, allow_null=True)
    idempotency_key = serializers.CharField(max_length=128)
    top_k = serializers.IntegerField(min_value=1, max_value=20, default=5)


class PolicyCategorySerializer(serializers.Serializer):
    kind = serializers.ChoiceField(
        choices=["policy", "employee", "location"], default="policy"
    )
    code = serializers.SlugField(max_length=80)
    name = serializers.CharField(max_length=150)


class PolicyRevisionSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=1)


class PolicySettingsSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=1)
    precedence = serializers.ListField(
        child=serializers.ChoiceField(
            choices=["priority", "department", "employee_category", "location"]
        ),
        max_length=4,
    )

    def validate_precedence(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("Precedence dimensions must be distinct.")
        return value
