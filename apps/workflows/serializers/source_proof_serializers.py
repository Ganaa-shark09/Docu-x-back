from rest_framework import serializers
from apps.workflows.serializers.workflow_serializers import StrictSerializer

from apps.workflows.services.proof_source_service import KINDS


class EvidenceCaptureSerializer(StrictSerializer):
    kind = serializers.ChoiceField(choices=KINDS)
    source_uuid = serializers.UUIDField()
    locator_type = serializers.ChoiceField(
        choices=[
            "source",
            "text_offsets",
            "page",
            "chunk",
            "output_path",
            "dataset_row",
            "transaction",
        ]
    )
    locator = serializers.JSONField(default=dict)
    precision = serializers.ChoiceField(
        choices=["exact", "approximate", "structured_record", "derived", "aggregate"]
    )
    excerpt = serializers.CharField(
        max_length=8000, required=False, trim_whitespace=False
    )
    # Method, disclosure and validity are server controlled for public commands.


class ProofOutputSerializer(StrictSerializer):
    kind = serializers.ChoiceField(choices=[k for k in KINDS if k != "document"])
    source_uuid = serializers.UUIDField()
    path = serializers.ListField(
        child=serializers.JSONField(), min_length=1, max_length=12
    )


class ProofReviewSerializer(StrictSerializer):
    revision = serializers.IntegerField(min_value=0)
    decision = serializers.ChoiceField(choices=["confirmed", "rejected"])
    reason = serializers.CharField(max_length=2000)
    idempotency_key = serializers.CharField(max_length=128)


class ProofCorrectionSerializer(StrictSerializer):
    revision = serializers.IntegerField(min_value=1)
    value = serializers.JSONField(allow_null=True)
    reason = serializers.CharField(max_length=2000)
    idempotency_key = serializers.CharField(max_length=128)
    evidence_uuids = serializers.ListField(
        child=serializers.UUIDField(), max_length=20, required=False
    )


class ProofMessageSerializer(StrictSerializer):
    message_uuid = serializers.UUIDField()


class ProofExportSerializer(StrictSerializer):
    external = serializers.BooleanField(default=False)
