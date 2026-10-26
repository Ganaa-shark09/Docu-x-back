from django.db import models

from apps.ai_engine.models.ai_conversation import AIConversation
from apps.shared.models import BaseModel


class AIMessage(BaseModel):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        SYSTEM = "system", "System"

    conversation = models.ForeignKey(
        AIConversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )

    role = models.CharField(
        max_length=50,
        choices=Role.choices,
        db_index=True,
    )

    content = models.TextField()

    retrieved_sources = models.JSONField(default=list, blank=True)
    model_provider = models.CharField(max_length=100, blank=True)
    model_name = models.CharField(max_length=150, blank=True)

    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)

    metadata = models.JSONField(default=dict, blank=True)

    def save(self, *args, **kwargs):
        if self._state.adding:
            from apps.ai_engine.services.message_visibility_service import (
                MessageVisibilityService,
            )

            self.metadata = dict(self.metadata or {})
            self.metadata["document_dependencies"] = MessageVisibilityService.capture(
                self
            )
            from apps.ai_engine.services.internal_source_proof_service import (
                InternalSourceProofService,
            )

            pins = InternalSourceProofService.capture(self)
            if pins:
                self.metadata["source_proof_pins"] = pins
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.conversation.uuid} - {self.role}"

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["conversation", "role"]),
            models.Index(fields=["created_at"]),
        ]
