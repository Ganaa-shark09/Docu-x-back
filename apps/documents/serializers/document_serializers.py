from rest_framework import serializers

from apps.data_firewall.models import FirewallScan
from apps.document_processing.models import DocumentTextExtraction
from apps.documents.models import (
    Document,
    DocumentAccessPolicy,
    DocumentProcessingJob,
    DocumentVersion,
)
from apps.documents.services import (
    DocumentFileValidationService,
    DocumentPermissionService,
    DocumentProcessingJobService,
)
from apps.workspaces.models import Department


class DocumentProcessingJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentProcessingJob
        fields = [
            "uuid",
            "job_type",
            "status",
            "started_at",
            "completed_at",
            "progress_percent",
            "error_message",
            "input_payload",
            "output_payload",
            "celery_task_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class DocumentVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentVersion
        fields = [
            "uuid",
            "version_number",
            "original_filename",
            "file_size",
            "mime_type",
            "checksum",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class DocumentAccessPolicySerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = DocumentAccessPolicy
        fields = [
            "uuid",
            "user",
            "user_email",
            "role",
            "access_level",
            "can_download",
            "can_export",
            "can_use_ai",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "uuid",
            "user_email",
            "created_at",
            "updated_at",
        ]

    def validate_user(self, user):
        request = self.context.get("request")

        if user and request and user.company_id != request.user.company_id:
            raise serializers.ValidationError("User does not belong to your company.")

        return user

    def validate(self, attrs):
        user = attrs.get("user")
        role = attrs.get("role")

        if not user and not role:
            raise serializers.ValidationError("Either user or role is required.")

        return attrs


class DocumentTextExtractionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentTextExtraction
        fields = [
            "uuid",
            "extraction_method",
            "page_count",
            "character_count",
            "word_count",
            "confidence_score",
            "metadata",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class FirewallScanSerializer(serializers.ModelSerializer):
    class Meta:
        model = FirewallScan
        fields = [
            "uuid",
            "risk_level",
            "decision",
            "detected_patterns",
            "redacted_preview",
            "scan_summary",
            "can_use_cloud_llm",
            "requires_human_approval",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class DocumentListSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    uploaded_by_email = serializers.EmailField(
        source="uploaded_by.email", read_only=True
    )
    current_user_access = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "uuid",
            "title",
            "description",
            "company_name",
            "department",
            "department_name",
            "uploaded_by_email",
            "current_user_access",
            "document_type",
            "status",
            "sensitivity_label",
            "original_filename",
            "file_size",
            "mime_type",
            "page_count",
            "language",
            "is_indexed",
            "is_ocr_completed",
            "is_ai_ready",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_current_user_access(self, obj):
        request = self.context.get("request")

        if not request:
            return None

        return DocumentPermissionService.get_highest_access_level(request.user, obj)


class DocumentDetailSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    uploaded_by_email = serializers.EmailField(
        source="uploaded_by.email", read_only=True
    )
    current_user_access = serializers.SerializerMethodField()
    versions = DocumentVersionSerializer(many=True, read_only=True)
    access_policies = DocumentAccessPolicySerializer(many=True, read_only=True)
    processing_jobs = DocumentProcessingJobSerializer(many=True, read_only=True)
    text_extraction = DocumentTextExtractionSerializer(read_only=True)
    firewall_scan = FirewallScanSerializer(read_only=True)

    class Meta:
        model = Document
        fields = [
            "uuid",
            "title",
            "description",
            "company_name",
            "department",
            "department_name",
            "uploaded_by_email",
            "current_user_access",
            "document_type",
            "status",
            "sensitivity_label",
            "original_filename",
            "file",
            "file_size",
            "mime_type",
            "checksum",
            "page_count",
            "language",
            "is_indexed",
            "is_ocr_completed",
            "is_ai_ready",
            "processing_error",
            "versions",
            "access_policies",
            "processing_jobs",
            "text_extraction",
            "firewall_scan",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "uuid",
            "company_name",
            "department_name",
            "uploaded_by_email",
            "current_user_access",
            "status",
            "original_filename",
            "file_size",
            "mime_type",
            "checksum",
            "page_count",
            "language",
            "is_indexed",
            "is_ocr_completed",
            "is_ai_ready",
            "processing_error",
            "versions",
            "access_policies",
            "processing_jobs",
            "text_extraction",
            "firewall_scan",
            "created_at",
            "updated_at",
        ]

    def get_current_user_access(self, obj):
        request = self.context.get("request")

        if not request:
            return None

        return DocumentPermissionService.get_highest_access_level(request.user, obj)


class DocumentCreateSerializer(serializers.ModelSerializer):
    department = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = Document
        fields = [
            "title",
            "description",
            "department",
            "document_type",
            "sensitivity_label",
            "file",
        ]

    def validate_department(self, department_uuid):
        request = self.context.get("request")

        if not department_uuid:
            return None

        department = Department.objects.filter(
            uuid=department_uuid,
            company=request.user.company,
            is_deleted=False,
        ).first()

        if not department:
            raise serializers.ValidationError("Department not found for your company.")

        return department

    def validate_file(self, uploaded_file):
        DocumentFileValidationService.validate_file(uploaded_file)
        return uploaded_file

    def create(self, validated_data):
        request = self.context["request"]
        uploaded_file = validated_data["file"]
        department = validated_data.pop("department", None)

        checksum = DocumentFileValidationService.calculate_checksum(uploaded_file)

        document = Document.objects.create(
            company=request.user.company,
            scope=Document.Scope.INTERNAL,
            department=department,
            uploaded_by=request.user,
            original_filename=uploaded_file.name,
            file_size=uploaded_file.size,
            mime_type=getattr(uploaded_file, "content_type", ""),
            checksum=checksum,
            **validated_data,
        )

        DocumentVersion.objects.create(
            document=document,
            version_number=1,
            uploaded_by=request.user,
            file=document.file,
            original_filename=document.original_filename,
            file_size=document.file_size,
            mime_type=document.mime_type,
            checksum=document.checksum,
            notes="Initial upload",
        )

        DocumentProcessingJobService.create_default_jobs_for_document(document)

        from apps.documents.tasks import process_document_pipeline

        process_document_pipeline.delay(str(document.uuid))

        return document


class DocumentUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = [
            "title",
            "description",
            "department",
            "document_type",
            "sensitivity_label",
            "status",
        ]

    def validate_department(self, department):
        request = self.context["request"]

        if department and department.company_id != request.user.company_id:
            raise serializers.ValidationError(
                "Selected department does not belong to your company."
            )

        return department
