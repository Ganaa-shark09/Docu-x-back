from django.core.exceptions import ValidationError
from django.db import models

from apps.ai_engine.models.ai_conversation import AIConversation
from apps.documents.models import Document
from apps.shared.models import BaseModel
from apps.tenants.models import Company


class AIConversationDocument(BaseModel):
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="ai_conversation_documents",
    )

    conversation = models.ForeignKey(
        AIConversation,
        on_delete=models.CASCADE,
        related_name="attached_documents",
    )

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="ai_conversation_attachments",
    )

    uploaded_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_conversation_document_uploads",
    )

    def clean(self):
        if self.conversation.company_id != self.company_id:
            raise ValidationError("Conversation must belong to the same company.")

        if self.document.company_id != self.company_id:
            raise ValidationError("Document must belong to the same company.")

        if self.conversation.scope != AIConversation.Scope.EXTERNAL:
            raise ValidationError(
                "Only external conversations can have uploaded documents."
            )

        if self.document.scope != Document.Scope.EXTERNAL:
            raise ValidationError("Only external documents can be attached.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.conversation.uuid} -> {self.document.title}"

    class Meta:
        unique_together = ("conversation", "document")
        indexes = [
            models.Index(fields=["company", "conversation"]),
            models.Index(fields=["company", "document"]),
        ]
