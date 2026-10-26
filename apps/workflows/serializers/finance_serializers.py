from rest_framework import serializers

from apps.workflows.serializers.rfp_serializers import StrictSerializer


class FinanceCreateSerializer(StrictSerializer):
    title = serializers.CharField(max_length=200)
    department = serializers.IntegerField(min_value=1, allow_null=True, required=False)
    scope = serializers.JSONField()
    policy = serializers.JSONField(required=False)


class FinanceRevisionSerializer(StrictSerializer):
    revision = serializers.IntegerField(min_value=1)


class FinanceConfigureSerializer(FinanceRevisionSerializer):
    scope = serializers.JSONField()
    policy = serializers.JSONField()


class FinanceImportSerializer(FinanceRevisionSerializer):
    document_uuid = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=["csv", "json"])
    mapping = serializers.JSONField(default=dict)
    control_totals = serializers.JSONField(required=False)
    idempotency_key = serializers.CharField(max_length=128)


class FinanceCorrectionSerializer(FinanceRevisionSerializer):
    values = serializers.JSONField()
    reason = serializers.CharField(max_length=1000)


class FinanceSupportSerializer(FinanceRevisionSerializer):
    document_uuid = serializers.UUIDField()
    role = serializers.ChoiceField(
        choices=["receipt", "purchase_order", "support", "approval", "contract"]
    )
    evidence = serializers.JSONField()


class FinanceSampleSerializer(FinanceRevisionSerializer):
    parameters = serializers.JSONField()
    idempotency_key = serializers.CharField(max_length=128)


class FinanceTestSerializer(FinanceRevisionSerializer):
    idempotency_key = serializers.CharField(max_length=128)
    sample_uuid = serializers.UUIDField(required=False, allow_null=True)
    previous_uuid = serializers.UUIDField(required=False, allow_null=True)


class FinanceSubmitSerializer(FinanceRevisionSerializer):
    reviewer = serializers.JSONField()
    approver = serializers.JSONField()
    idempotency_key = serializers.CharField(max_length=128)


class FinanceResponseSerializer(FinanceRevisionSerializer):
    request_uuid = serializers.UUIDField(required=False)
    response = serializers.CharField(max_length=8000)
    planned_action = serializers.CharField(max_length=8000)
    target_date = serializers.CharField(max_length=10, required=False)
    owner = serializers.JSONField(required=False)
    evidence_documents = serializers.ListField(
        child=serializers.UUIDField(), max_length=20, required=False
    )


class FinanceDecisionSerializer(FinanceRevisionSerializer):
    decision = serializers.ChoiceField(
        choices=[
            "under_review",
            "remediation_in_progress",
            "resolved",
            "accepted_risk",
            "closed",
        ]
    )
    reason = serializers.CharField(max_length=1000)


class FinanceEvidenceRequestSerializer(FinanceRevisionSerializer):
    requested_item = serializers.CharField(max_length=2000)
    owner = serializers.JSONField()
    due_date = serializers.CharField(max_length=10, required=False)
