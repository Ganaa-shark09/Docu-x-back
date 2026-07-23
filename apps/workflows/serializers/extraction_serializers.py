from django.utils import timezone
from rest_framework import serializers

from apps.accounts.models import User
from apps.data_firewall.models import FirewallScan
from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService
from apps.workflows.models import (
    ExtractionResultField,
    ExtractionRun,
    ExtractionTemplate,
    ExtractionTemplateField,
)


class ExtractionTemplateFieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExtractionTemplateField
        fields = [
            "uuid",
            "field_key",
            "label",
            "field_type",
            "description",
            "extraction_hint",
            "regex_pattern",
            "is_required",
            "order",
            "default_value",
            "validation_rules",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "uuid",
            "created_at",
            "updated_at",
        ]


class ExtractionTemplateSerializer(serializers.ModelSerializer):
    fields = ExtractionTemplateFieldSerializer(many=True, read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)

    class Meta:
        model = ExtractionTemplate
        fields = [
            "uuid",
            "company_name",
            "name",
            "template_type",
            "description",
            "is_system",
            "is_active",
            "output_schema",
            "instructions",
            "fields",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "uuid",
            "company_name",
            "is_system",
            "created_at",
            "updated_at",
        ]

    def create(self, validated_data):
        request = self.context["request"]
        return ExtractionTemplate.objects.create(
            company=request.user.company,
            is_system=False,
            **validated_data,
        )


class ExtractionResultFieldSerializer(serializers.ModelSerializer):
    final_value = serializers.SerializerMethodField()

    class Meta:
        model = ExtractionResultField
        fields = [
            "uuid",
            "field_key",
            "label",
            "extracted_value",
            "normalized_value",
            "confidence_score",
            "source_text",
            "source_metadata",
            "is_required",
            "is_missing",
            "is_corrected",
            "corrected_value",
            "final_value",
            "reviewed_by",
            "reviewed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "uuid",
            "field_key",
            "label",
            "extracted_value",
            "normalized_value",
            "confidence_score",
            "source_text",
            "source_metadata",
            "is_required",
            "is_missing",
            "final_value",
            "reviewed_by",
            "reviewed_at",
            "created_at",
            "updated_at",
        ]

    def get_final_value(self, obj):
        return obj.final_value()


class ExtractionRunListSerializer(serializers.ModelSerializer):
    document_uuid = serializers.UUIDField(source="document.uuid", read_only=True)
    document_title = serializers.CharField(source="document.title", read_only=True)
    template_name = serializers.CharField(source="template.name", read_only=True)
    requested_by_email = serializers.EmailField(
        source="requested_by.email", read_only=True
    )

    class Meta:
        model = ExtractionRun
        fields = [
            "uuid",
            "document_uuid",
            "document_title",
            "template",
            "template_name",
            "requested_by",
            "requested_by_email",
            "status",
            "overall_confidence",
            "approval_request_uuid",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ExtractionRunDetailSerializer(serializers.ModelSerializer):
    document_uuid = serializers.UUIDField(source="document.uuid", read_only=True)
    document_title = serializers.CharField(source="document.title", read_only=True)
    template_name = serializers.CharField(source="template.name", read_only=True)
    result_fields = ExtractionResultFieldSerializer(many=True, read_only=True)

    class Meta:
        model = ExtractionRun
        fields = [
            "uuid",
            "document",
            "document_uuid",
            "document_title",
            "template",
            "template_name",
            "requested_by",
            "status",
            "overall_confidence",
            "raw_output",
            "error_message",
            "approval_request_uuid",
            "started_at",
            "completed_at",
            "result_fields",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ExtractionRunCreateSerializer(serializers.Serializer):
    document = serializers.UUIDField()
    template = serializers.UUIDField()

    def validate(self, attrs):
        request = self.context["request"]

        document = Document.objects.filter(
            uuid=attrs["document"],
            company=request.user.company,
            is_deleted=False,
        ).first()

        if not document:
            raise serializers.ValidationError({"document": "Document not found."})

        if not DocumentPermissionService.has_document_access(
            user=request.user,
            document=document,
            required_access="review",
            require_ai=True,
        ):
            raise serializers.ValidationError(
                {
                    "document": "You do not have extraction/review access for this document."
                }
            )

        if document.status != Document.Status.READY or not document.is_ocr_completed:
            raise serializers.ValidationError(
                {
                    "document": "Document must be processed and OCR completed before extraction."
                }
            )

        if document.sensitivity_label in [
            Document.Sensitivity.RESTRICTED,
            Document.Sensitivity.HIGHLY_CONFIDENTIAL,
        ]:
            raise serializers.ValidationError(
                {
                    "document": (
                        "Structured extraction is blocked for restricted or highly confidential "
                        "documents until a dedicated approval workflow is completed."
                    )
                }
            )

        firewall_scan = getattr(document, "firewall_scan", None)

        if firewall_scan and firewall_scan.requires_human_approval:
            raise serializers.ValidationError(
                {
                    "document": (
                        "This document requires human firewall approval before structured extraction."
                    )
                }
            )

        if (
            firewall_scan
            and firewall_scan.decision == FirewallScan.Decision.BLOCK_CLOUD_LLM
        ):
            raise serializers.ValidationError(
                {
                    "document": (
                        "This document is blocked by AI Data Firewall because critical sensitive "
                        "content was detected."
                    )
                }
            )

        template = (
            ExtractionTemplate.objects.filter(
                uuid=attrs["template"],
                is_active=True,
                is_deleted=False,
            )
            .filter(company=request.user.company)
            .first()
        )

        if not template:
            template = ExtractionTemplate.objects.filter(
                uuid=attrs["template"],
                is_system=True,
                is_active=True,
                is_deleted=False,
            ).first()

        if not template:
            raise serializers.ValidationError(
                {"template": "Extraction template not found."}
            )

        if (
            template.template_type != ExtractionTemplate.TemplateType.CUSTOM
            and document.document_type != template.template_type
        ):
            raise serializers.ValidationError(
                {
                    "template": (
                        f"Template type '{template.template_type}' cannot be used for "
                        f"document type '{document.document_type}'."
                    )
                }
            )

        attrs["document_obj"] = document
        attrs["template_obj"] = template

        return attrs

    def create(self, validated_data):
        request = self.context["request"]

        extraction_run = ExtractionRun.objects.create(
            company=request.user.company,
            document=validated_data["document_obj"],
            template=validated_data["template_obj"],
            requested_by=request.user,
            status=ExtractionRun.Status.PENDING,
        )

        from apps.workflows.tasks import run_structured_extraction_task

        run_structured_extraction_task.delay(str(extraction_run.uuid))

        return extraction_run


class ExtractionFieldCorrectionSerializer(serializers.Serializer):
    corrected_value = serializers.CharField(allow_blank=True)

    def update(self, instance, validated_data):
        request = self.context["request"]

        instance.corrected_value = validated_data["corrected_value"]
        instance.is_corrected = True
        instance.reviewed_by = request.user
        instance.reviewed_at = timezone.now()
        instance.is_missing = not bool(instance.corrected_value.strip())
        instance.save(
            update_fields=[
                "corrected_value",
                "is_corrected",
                "reviewed_by",
                "reviewed_at",
                "is_missing",
                "updated_at",
            ]
        )
        return instance


class SubmitExtractionForApprovalSerializer(serializers.Serializer):
    assigned_to = serializers.IntegerField(required=False, allow_null=True)

    def validate_assigned_to(self, assigned_to_id):
        request = self.context["request"]

        if not assigned_to_id:
            return None

        user = User.objects.filter(
            id=assigned_to_id,
            company=request.user.company,
            is_active=True,
        ).first()

        if not user:
            raise serializers.ValidationError(
                "Assigned reviewer not found in your company."
            )

        return user
