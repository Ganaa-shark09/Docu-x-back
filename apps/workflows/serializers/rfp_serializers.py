from rest_framework import serializers

from apps.workflows.services.rfp_service import COMPLIANCE, ROLES


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError(
                {"non_field_errors": ["Unknown command fields."]}
            )
        return super().to_internal_value(data)


class RFPSourceSerializer(StrictSerializer):
    document_uuid = serializers.UUIDField()
    role = serializers.ChoiceField(choices=sorted(ROLES))


class RFPCreateSerializer(StrictSerializer):
    finalization = serializers.DictField(required=False)
    title = serializers.CharField(max_length=200)
    department = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    sources = RFPSourceSerializer(many=True)
    knowledge_document_uuids = serializers.ListField(
        child=serializers.UUIDField(), max_length=20, required=False
    )


class RFPRevisionSerializer(StrictSerializer):
    revision = serializers.IntegerField(min_value=1)


class RFPSubmitSerializer(RFPRevisionSerializer):
    reviewer = serializers.JSONField()
    approver = serializers.JSONField()
    idempotency_key = serializers.CharField(max_length=128)


class RFPResponseSerializer(RFPRevisionSerializer):
    text = serializers.CharField(max_length=12000, allow_blank=True)
    evidence = serializers.ListField(child=serializers.JSONField(), max_length=20)
    compliance = serializers.ChoiceField(choices=sorted(COMPLIANCE))
    reason = serializers.CharField(max_length=500)
    externally_shareable = serializers.BooleanField(default=False)
    assignment = serializers.JSONField(required=False)
    category = serializers.SlugField(max_length=80, default="unclassified")


class RFPAttachmentSerializer(RFPRevisionSerializer):
    document_uuid = serializers.UUIDField()
    assignment = serializers.JSONField(required=False)


class RFPExportSerializer(StrictSerializer):
    external = serializers.BooleanField(default=True)


class RFPCorrectionSerializer(RFPRevisionSerializer):
    source_uuid = serializers.UUIDField()
    kind = serializers.ChoiceField(
        choices=["classification", "deadline", "requirement", "legal_analysis"]
    )
    key = serializers.CharField(max_length=80)
    value = serializers.JSONField()
    evidence = serializers.JSONField()
    reason = serializers.CharField(max_length=500)
