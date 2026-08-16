from rest_framework import serializers

from apps.ai_engine.models import AIConversation
from apps.documents.models import Document
from apps.documents.services.external_document_upload_service import (
    ExternalDocumentUploadService,
)


class ExternalDocumentSerializer(serializers.ModelSerializer):
    uploaded_by_email = serializers.EmailField(
        source="uploaded_by.email", read_only=True
    )
    conversation_uuid = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "uuid",
            "conversation_uuid",
            "scope",
            "title",
            "description",
            "document_type",
            "sensitivity_label",
            "original_filename",
            "file_size",
            "mime_type",
            "status",
            "processing_stage",
            "processing_progress",
            "is_ocr_completed",
            "is_indexed",
            "is_ai_ready",
            "processing_error",
            "uploaded_by_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_conversation_uuid(self, obj):
        if hasattr(obj, "_external_conversation_uuid"):
            return obj._external_conversation_uuid

        attachment = (
            obj.ai_conversation_attachments.filter(
                is_deleted=False,
            )
            .select_related("conversation")
            .first()
        )

        if not attachment:
            return None

        return str(attachment.conversation.uuid)


class ExternalDocumentUploadSerializer(serializers.ModelSerializer):
    conversation_uuid = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = Document
        fields = [
            "conversation_uuid",
            "title",
            "description",
            "document_type",
            "sensitivity_label",
            "file",
        ]

    def validate_conversation_uuid(self, conversation_uuid):
        if not conversation_uuid:
            return None

        request = self.context["request"]

        conversation = AIConversation.objects.filter(
            uuid=conversation_uuid,
            company=request.user.company,
            user=request.user,
            scope=AIConversation.Scope.EXTERNAL,
            is_deleted=False,
        ).first()

        if not conversation:
            raise serializers.ValidationError("External conversation not found.")

        return conversation.uuid

    def create(self, validated_data):
        request = self.context["request"]
        uploaded_file = validated_data["file"]

        document, conversation = ExternalDocumentUploadService.upload_external_document(
            user=request.user,
            uploaded_file=uploaded_file,
            conversation_uuid=validated_data.get("conversation_uuid"),
            title=validated_data.get("title") or uploaded_file.name,
            description=validated_data.get("description", ""),
            document_type=validated_data.get("document_type")
            or Document.DocumentType.OTHER,
            sensitivity_label=validated_data.get(
                "sensitivity_label",
                Document.Sensitivity.INTERNAL,
            ),
        )

        document._external_conversation_uuid = str(conversation.uuid)

        return document
