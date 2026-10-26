from rest_framework import serializers

from apps.workflows.models import (
    ExtractionCorrection,
    InvoiceOutputVersion,
    InvoiceRecord,
    InvoiceBatch,
    InvoiceBatchItem,
)
from apps.workflows.serializers.workflow_serializers import StrictSerializer
from apps.workflows.services.invoice_schema import InvoicePolicy
from apps.workflows.services.invoice_workflow_service import InvoiceWorkflowService
from apps.workflows.services.invoice_matching_service import (
    accessible_invoice_records,
)


class InvoiceBatchCreateSerializer(StrictSerializer):
    name = serializers.CharField(max_length=150)
    version = serializers.UUIDField()
    documents = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=500
    )
    idempotency_key = serializers.CharField(max_length=128)


class InvoiceAccountingPostSerializer(StrictSerializer):
    idempotency_key = serializers.CharField(max_length=128)


def invoice_batch_item_status(item):
    if item.run_id:
        return item.run.status
    return "queued" if item.error_code == "awaiting_document" else "failed"


class InvoiceBatchItemSerializer(serializers.ModelSerializer):
    document_uuid = serializers.UUIDField(source="document.uuid", read_only=True)
    document_title = serializers.CharField(source="document.title", read_only=True)
    run_uuid = serializers.UUIDField(source="run.uuid", read_only=True, allow_null=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = InvoiceBatchItem
        fields = [
            "uuid",
            "position",
            "document_uuid",
            "document_title",
            "run_uuid",
            "status",
            "error_code",
            "error_message",
        ]
        read_only_fields = fields

    def get_status(self, obj):
        return invoice_batch_item_status(obj)


class InvoiceBatchSerializer(serializers.ModelSerializer):
    version_uuid = serializers.UUIDField(source="version.uuid", read_only=True)
    items = InvoiceBatchItemSerializer(many=True, read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta:
        model = InvoiceBatch
        fields = ["uuid", "name", "version_uuid", "created_at", "items", "progress"]
        read_only_fields = fields

    def get_progress(self, obj):
        items = list(obj.items.all())
        counts = {
            "total": len(items),
            "completed": 0,
            "review": 0,
            "failed": 0,
            "processing": 0,
        }
        for item in items:
            status = invoice_batch_item_status(item)
            if status in {"completed", "approved"}:
                counts["completed"] += 1
            elif status in {"review_required", "approval_required", "exception"}:
                counts["review"] += 1
            elif status in {"failed", "rejected", "cancelled"}:
                counts["failed"] += 1
            else:
                counts["processing"] += 1
        return counts


class InvoiceRegisterSerializer(serializers.ModelSerializer):
    """Bounded invoice summaries for the access-controlled AP register."""

    workflow_uuid = serializers.UUIDField(source="workflow_run.uuid", read_only=True)
    document_uuid = serializers.UUIDField(source="document.uuid", read_only=True)
    document_title = serializers.CharField(source="document.title", read_only=True)
    status = serializers.CharField(source="workflow_run.status", read_only=True)
    vendor_name = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()
    grand_total = serializers.SerializerMethodField()
    open_issues = serializers.SerializerMethodField()
    has_final_output = serializers.BooleanField(read_only=True)

    class Meta:
        model = InvoiceRecord
        fields = [
            "uuid",
            "workflow_uuid",
            "document_uuid",
            "document_title",
            "status",
            "revision",
            "invoice_number",
            "invoice_date",
            "vendor_name",
            "currency",
            "grand_total",
            "open_issues",
            "has_final_output",
            "effective_confidence",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_vendor_name(self, obj):
        return (obj.normalized or {}).get("vendor_name")

    def get_currency(self, obj):
        return (obj.normalized or {}).get("currency")

    def get_grand_total(self, obj):
        return (obj.normalized or {}).get("grand_total")

    def get_open_issues(self, obj):
        return sum(
            1
            for issue in (obj.validation or [])
            if isinstance(issue, dict) and issue.get("blocking") is True
        )


class InvoiceDefinitionSerializer(StrictSerializer):
    name = serializers.CharField(max_length=150)
    code = serializers.SlugField(max_length=80)
    reviewer = serializers.DictField()
    approver = serializers.DictField(required=False)
    department = serializers.IntegerField(min_value=1, allow_null=True, required=False)
    policy = serializers.DictField(required=False)

    def validate_policy(self, value):
        return InvoicePolicy.validate(value)


class InvoiceCorrectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExtractionCorrection
        fields = [
            "uuid",
            "actor",
            "revision",
            "previous_value",
            "corrected_value",
            "reason",
            "created_at",
        ]
        read_only_fields = fields


class InvoiceOutputSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceOutputVersion
        fields = ["uuid", "revision", "schema_version", "digest", "data", "created_at"]
        read_only_fields = fields


class InvoiceSerializer(serializers.ModelSerializer):
    workflow_uuid = serializers.UUIDField(source="workflow_run.uuid", read_only=True)
    document_uuid = serializers.UUIDField(source="document.uuid", read_only=True)
    extraction_uuid = serializers.UUIDField(
        source="extraction_run.uuid", read_only=True, allow_null=True
    )
    field_provenance = serializers.SerializerMethodField()
    duplicate_candidates = serializers.SerializerMethodField()
    final_output = serializers.SerializerMethodField()

    class Meta:
        model = InvoiceRecord
        fields = [
            "uuid",
            "workflow_uuid",
            "document_uuid",
            "extraction_uuid",
            "revision",
            "normalized",
            "classification",
            "validation",
            "matches",
            "effective_confidence",
            "field_provenance",
            "duplicate_candidates",
            "final_output",
        ]
        read_only_fields = fields

    def get_field_provenance(self, obj):
        return InvoiceWorkflowService.provenance(obj)

    def get_duplicate_candidates(self, obj):
        user = self.context["request"].user
        allowed = set(
            map(
                str,
                accessible_invoice_records(user)
                .filter(uuid__in=[x["invoice_uuid"] for x in obj.duplicates])
                .values_list("uuid", flat=True),
            )
        )
        return [x for x in obj.duplicates if x["invoice_uuid"] in allowed]

    def get_final_output(self, obj):
        output = obj.outputs.filter(is_deleted=False).first()
        return InvoiceOutputSerializer(output).data if output else None
