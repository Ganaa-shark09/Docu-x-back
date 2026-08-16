from apps.ai_engine.models import AIConversation, AIConversationDocument
from apps.documents.models import Document, DocumentVersion
from apps.documents.services import (
    DocumentFileValidationService,
    DocumentProcessingJobService,
    DocumentProcessingQueueService,
)


class ExternalDocumentUploadService:
    @classmethod
    def get_or_create_external_conversation(
        cls,
        user,
        conversation_uuid=None,
        title=None,
    ):
        if conversation_uuid:
            conversation = AIConversation.objects.filter(
                uuid=conversation_uuid,
                company=user.company,
                user=user,
                scope=AIConversation.Scope.EXTERNAL,
                is_deleted=False,
            ).first()

            if not conversation:
                raise ValueError("External conversation not found.")

            return conversation

        return AIConversation.objects.create(
            company=user.company,
            user=user,
            scope=AIConversation.Scope.EXTERNAL,
            title=(title or "External file upload")[:120],
        )

    @classmethod
    def upload_external_document(
        cls,
        user,
        uploaded_file,
        conversation=None,
        conversation_uuid=None,
        title=None,
        description="",
        document_type=None,
        sensitivity_label=None,
    ):
        DocumentFileValidationService.validate_file(uploaded_file)

        conversation = conversation or cls.get_or_create_external_conversation(
            user=user,
            conversation_uuid=conversation_uuid,
            title=title or uploaded_file.name,
        )

        checksum = DocumentFileValidationService.calculate_checksum(uploaded_file)

        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)

        document = Document.objects.create(
            company=user.company,
            scope=Document.Scope.EXTERNAL,
            department=None,
            uploaded_by=user,
            title=title or uploaded_file.name,
            description=description or "",
            document_type=document_type or Document.DocumentType.OTHER,
            sensitivity_label=sensitivity_label or Document.Sensitivity.INTERNAL,
            file=uploaded_file,
            original_filename=uploaded_file.name,
            file_size=uploaded_file.size,
            mime_type=getattr(uploaded_file, "content_type", ""),
            checksum=checksum,
        )

        DocumentVersion.objects.create(
            document=document,
            version_number=1,
            uploaded_by=user,
            file=document.file,
            original_filename=document.original_filename,
            file_size=document.file_size,
            mime_type=document.mime_type,
            checksum=document.checksum,
            notes="External chat upload",
        )

        AIConversationDocument.objects.get_or_create(
            company=user.company,
            conversation=conversation,
            document=document,
            defaults={"uploaded_by": user},
        )

        DocumentProcessingJobService.create_default_jobs_for_document(document)

        DocumentProcessingQueueService.enqueue(document)

        document._external_conversation_uuid = str(conversation.uuid)

        return document, conversation
