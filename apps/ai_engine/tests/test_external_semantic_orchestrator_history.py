from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.ai_engine.models import AIConversation, AIMessage
from apps.ai_engine.services.external_semantic_orchestrator_service import (
    ExternalSemanticOrchestratorService,
)
from apps.tenants.models import Company


class ExternalSemanticOrchestratorHistoryTests(TestCase):
    def test_failed_clarification_assistant_message_is_not_reused_as_routing_history(
        self,
    ):
        company = Company.objects.create(name="History Co", slug="history-co")
        user = get_user_model().objects.create_user(
            username="history-user",
            password="test-password",
            company=company,
        )
        conversation = AIConversation.objects.create(
            company=company,
            user=user,
            scope=AIConversation.Scope.EXTERNAL,
            title="Screenshot chat",
        )

        AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.USER,
            content="Please explain this screenshot",
        )
        AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content="I cannot see or access the screenshot.",
            metadata={"semantic_action": "clarify"},
        )

        history = ExternalSemanticOrchestratorService._build_history(conversation)

        self.assertEqual(
            history,
            [{"role": "user", "content": "Please explain this screenshot"}],
        )
