from rest_framework import serializers
from apps.workflows.serializers.rfp_serializers import StrictSerializer


class FrameworkCreateSerializer(StrictSerializer):
    code = serializers.SlugField(max_length=80)
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(
        max_length=4000, required=False, allow_blank=True
    )
    source = serializers.CharField(max_length=500, required=False, allow_blank=True)
    jurisdiction = serializers.CharField(
        max_length=100, required=False, allow_blank=True
    )
    draft = serializers.JSONField()


class RevisionSerializer(StrictSerializer):
    revision = serializers.IntegerField(min_value=1)


class DraftSerializer(RevisionSerializer):
    draft = serializers.JSONField()


class ActivateSerializer(RevisionSerializer):
    version_uuid = serializers.UUIDField(allow_null=True)


class ControlCreateSerializer(StrictSerializer):
    code = serializers.SlugField(max_length=80)
    name = serializers.CharField(max_length=200)
    department = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    definition = serializers.JSONField()


class ControlUpdateSerializer(RevisionSerializer):
    name = serializers.CharField(max_length=200)
    definition = serializers.JSONField()
    is_active = serializers.BooleanField(default=True)


class MappingSerializer(RevisionSerializer):
    requirement_uuid = serializers.UUIDField()
    is_active = serializers.BooleanField(default=True)


class AssessmentCreateSerializer(StrictSerializer):
    title = serializers.CharField(max_length=200)
    department = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    framework_version_uuid = serializers.UUIDField()
    scope = serializers.JSONField()
    previous_uuid = serializers.UUIDField(required=False, allow_null=True)
    idempotency_key = serializers.CharField(max_length=128)


class SubmitSerializer(RevisionSerializer):
    reviewer = serializers.JSONField()
    approver = serializers.JSONField()
    idempotency_key = serializers.CharField(max_length=128)


class EvidenceSerializer(RevisionSerializer):
    requirement_uuid = serializers.UUIDField()
    control_uuid = serializers.UUIDField()
    source_kind = serializers.CharField(max_length=30)
    source_uuid = serializers.UUIDField()
    dates = serializers.JSONField(default=dict)
    location = serializers.JSONField(default=dict)
    externally_shareable = serializers.BooleanField(default=False)
    idempotency_key = serializers.CharField(max_length=128)


class EvaluationSerializer(RevisionSerializer):
    idempotency_key = serializers.CharField(max_length=128)


class DecisionSerializer(RevisionSerializer):
    kind = serializers.CharField(max_length=30)
    target = serializers.CharField(max_length=128)
    outcome = serializers.CharField(max_length=40)
    reason = serializers.CharField(max_length=2000)
    data = serializers.JSONField(default=dict)
    idempotency_key = serializers.CharField(max_length=128)


class DiscoverySerializer(StrictSerializer):
    query = serializers.CharField(max_length=200)


class ExportSerializer(StrictSerializer):
    output_uuid = serializers.UUIDField()
    external = serializers.BooleanField(default=False)
