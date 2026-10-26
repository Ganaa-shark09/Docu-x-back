from rest_framework import serializers

from apps.workflows.models import (
    ContractReview,
    ContractReviewOutput,
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
