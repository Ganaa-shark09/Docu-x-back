from rest_framework import serializers

from apps.workflows.serializers.workflow_serializers import StrictSerializer
from apps.workflows.serializers.contract_serializers import ContractDefinitionSerializer
from apps.workflows.services import clause_schema as validation
from apps.workflows.services.clause_definition_service import options


class ClauseDefinitionSerializer(ContractDefinitionSerializer):
    def validate_policy(self, value):
        return options(value)


class TaxonomyWriteSerializer(StrictSerializer):
    code = serializers.SlugField(max_length=80)
    name = serializers.CharField(max_length=150)
    category = serializers.SlugField(max_length=80)
    description = serializers.CharField(
        max_length=2000, allow_blank=True, required=False
    )
    aliases = serializers.ListField(
        child=serializers.CharField(max_length=150), max_length=64, required=False
    )
    document_types = serializers.ListField(
        child=serializers.CharField(max_length=80), max_length=32, required=False
    )
    fact_schema = serializers.DictField(required=False)
    is_active = serializers.BooleanField(required=False)

    def validate_code(self, value):
        return validation.identifier(value)

    def validate_aliases(self, value):
        return validation.strings(value)

    def validate_document_types(self, value):
        return validation.strings(value, 32, 80)

    def validate_fact_schema(self, value):
        return validation.schema(value)


class PlaybookCreateSerializer(StrictSerializer):
    name = serializers.CharField(max_length=150)
    description = serializers.CharField(
        max_length=2000, allow_blank=True, required=False
    )
    department = serializers.IntegerField(min_value=1, allow_null=True, required=False)
    draft = serializers.DictField(required=False)

    def validate_draft(self, value):
        return validation.bounded_json(value, 65536)


class RevisionSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=1)


class PlaybookDraftSerializer(RevisionSerializer):
    name = serializers.CharField(max_length=150, required=False)
    description = serializers.CharField(
        max_length=2000, allow_blank=True, required=False
    )
    draft = serializers.DictField(required=False)


class PlaybookActivationSerializer(RevisionSerializer):
    version = serializers.UUIDField(required=False)
    active = serializers.BooleanField()


class ClauseDecisionSerializer(RevisionSerializer):
    target = serializers.UUIDField()
    target_type = serializers.ChoiceField(choices=["clause", "deviation"])
    decision = serializers.ChoiceField(choices=sorted(validation.DECISIONS))
    reason = serializers.CharField(max_length=4000)
    idempotency_key = serializers.CharField(max_length=128)


class ClauseCorrectionSerializer(RevisionSerializer):
    clause = serializers.UUIDField()
    clause_type = serializers.UUIDField()
    facts = serializers.DictField()
    evidence = serializers.ListField(
        child=serializers.DictField(), min_length=1, max_length=20
    )
    reason = serializers.CharField(max_length=4000)


class ClauseReanalysisSerializer(StrictSerializer):
    version = serializers.UUIDField()
    idempotency_key = serializers.CharField(max_length=128)
