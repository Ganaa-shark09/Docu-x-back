from rest_framework import serializers

from apps.workflows.models import (
    ContractReview,
    ContractReviewOutput,
    ContractObligationAssignment,
    ExtractionCorrection,
)
from apps.workflows.serializers.workflow_serializers import StrictSerializer
from apps.workflows.services.contract_schema import ContractPolicy
from apps.workflows.services.contract_workflow_service import ContractWorkflowService


class ContractDefinitionSerializer(StrictSerializer):
    name = serializers.CharField(max_length=150)
    code = serializers.SlugField(max_length=80)
    reviewer = serializers.DictField()
    approver = serializers.DictField(required=False)
    department = serializers.IntegerField(min_value=1, allow_null=True, required=False)
    policy = serializers.DictField(required=False)

    def validate_policy(self, value):
        return ContractPolicy.validate(value)


class ContractCorrectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExtractionCorrection
        fields = [
            "uuid",
            "actor",
            "revision",
            "previous_value",
            "corrected_value",
            "reason",
            "source_metadata",
            "created_at",
        ]
        read_only_fields = fields


class ContractOutputSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContractReviewOutput
        fields = ["uuid", "revision", "schema_version", "digest", "data", "created_at"]
        read_only_fields = fields


class ContractRegisterSerializer(serializers.ModelSerializer):
    """Bounded register projection: no full clause text or field provenance."""

    workflow_uuid = serializers.UUIDField(source="workflow_run.uuid", read_only=True)
    document_uuid = serializers.UUIDField(source="document.uuid", read_only=True)
    document_title = serializers.CharField(source="document.title", read_only=True)
    status = serializers.CharField(source="workflow_run.status", read_only=True)
    contract_type = serializers.SerializerMethodField()
    effective_date = serializers.SerializerMethodField()
    expiration_date = serializers.SerializerMethodField()
    notice_deadline = serializers.SerializerMethodField()
    blocking_issues = serializers.SerializerMethodField()
    source_coverage_complete = serializers.SerializerMethodField()
    review_output_finalized_at = serializers.DateTimeField(
        source="finalized_at", read_only=True
    )

    class Meta:
        model = ContractReview
        fields = (
            "uuid", "workflow_uuid", "document_uuid", "document_title",
            "status", "revision", "contract_type", "effective_date",
            "expiration_date", "notice_deadline", "blocking_issues",
            "source_coverage_complete", "review_output_finalized_at", "created_at",
        )
        read_only_fields = fields

    def _fact(self, obj, key):
        value = obj.normalized.get(key) if isinstance(obj.normalized, dict) else None
        return value if isinstance(value, str) and value else None

    def get_contract_type(self, obj):
        return self._fact(obj, "contract_type")

    def get_effective_date(self, obj):
        return self._fact(obj, "effective_date")

    def get_expiration_date(self, obj):
        return self._fact(obj, "expiration_date")

    def get_notice_deadline(self, obj):
        return self._fact(obj, "notice_deadline")

    def get_blocking_issues(self, obj):
        issues = obj.validation if isinstance(obj.validation, list) else []
        return sum(
            isinstance(item, dict) and item.get("blocking") is True
            for item in issues
        )

    def get_source_coverage_complete(self, obj):
        if not isinstance(obj.coverage, dict) or "complete" not in obj.coverage:
            return None
        return obj.coverage["complete"] is True


class ContractReviewSerializer(serializers.ModelSerializer):
    workflow_uuid = serializers.UUIDField(source="workflow_run.uuid", read_only=True)
    document_uuid = serializers.UUIDField(source="document.uuid", read_only=True)
    extraction_uuid = serializers.UUIDField(
        source="extraction_run.uuid", read_only=True, allow_null=True
    )
    field_provenance = serializers.SerializerMethodField()
    final_output = serializers.SerializerMethodField()

    class Meta:
        model = ContractReview
        fields = [
            "uuid",
            "workflow_uuid",
            "document_uuid",
            "extraction_uuid",
            "revision",
            "normalized",
            "classification",
            "validation",
            "coverage",
            "effective_confidence",
            "finalized_at",
            "field_provenance",
            "final_output",
        ]
        read_only_fields = fields

    def get_field_provenance(self, obj):
        return ContractWorkflowService.provenance(obj)

    def get_final_output(self, obj):
        output = next(iter(obj.outputs.all()), None)
        return ContractOutputSerializer(output).data if output else None


class ContractObligationAssignSerializer(StrictSerializer):
    source_index = serializers.IntegerField(min_value=0, max_value=99)
    owner_id = serializers.IntegerField(min_value=1)
    expected_review_revision = serializers.IntegerField(min_value=1)


class ContractObligationTransitionSerializer(StrictSerializer):
    status = serializers.ChoiceField(choices=("in_progress", "completed"))
    reason = serializers.CharField(min_length=3, max_length=500, trim_whitespace=True)
    expected_revision = serializers.IntegerField(min_value=1)


class ContractObligationSerializer(serializers.ModelSerializer):
    owner_id = serializers.IntegerField(read_only=True)
    events = serializers.SerializerMethodField()
    stale = serializers.SerializerMethodField()

    class Meta:
        model = ContractObligationAssignment
        fields = (
            "uuid", "owner_id", "source_revision", "source_index",
            "source_digest", "source_snapshot", "governance_revision",
            "status", "revision", "stale", "events", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_events(self, obj):
        return [
            {"sequence": event.sequence, "from_status": event.from_status,
             "to_status": event.to_status, "reason": event.reason,
             "actor_id": event.actor_id,
             "created_at": event.created_at}
            for event in obj.events.all()
        ]

    def get_stale(self, obj):
        return (
            obj.source_revision != obj.review.revision
            or obj.governance_revision != obj.review.document.governance_revision
        )
