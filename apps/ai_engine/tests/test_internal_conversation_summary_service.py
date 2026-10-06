from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.ai_engine.services.internal_conversation_summary_service import (
    InternalConversationSummaryService,
)


class InternalConversationSummaryServiceTests(SimpleTestCase):
    @patch(
        (
            "apps.ai_engine.services."
            "internal_conversation_summary_service."
            "requests.post"
        )
    )
    @patch(
        (
            "apps.ai_engine.services."
            "internal_conversation_summary_service."
            "InternalConversationMemoryService."
            "build_history_messages"
        )
    )
    def test_summary_uses_permission_filtered_history_and_local_ollama(
        self,
        build_history,
        post,
    ):
        build_history.return_value = [
            {
                "role": "user",
                "content": "Ask about Contract A",
            },
            {
                "role": "assistant",
                "content": "Discussed Contract A",
            },
            {
                "role": "user",
                "content": "Then ask about Policy B",
            },
        ]

        response = Mock()

        response.raise_for_status.return_value = None

        response.json.return_value = {
            "message": {
                "content": ("The conversation covered " "Contract A and Policy B.")
            },
            "prompt_eval_count": 20,
            "eval_count": 10,
        }

        post.return_value = response

        result = InternalConversationSummaryService.generate(
            user=SimpleNamespace(),
            conversation=SimpleNamespace(),
            current_user_message_uuid="current",
            message="Summarize our chat",
        )

        self.assertEqual(
            result["provider"],
            "ollama",
        )

        self.assertTrue(result["metadata"]["local_only"])

        payload = post.call_args.kwargs["json"]

        self.assertFalse(payload["think"])

        prompt = payload["messages"][1]["content"]

        self.assertIn(
            "Contract A",
            prompt,
        )

        self.assertIn(
            "Policy B",
            prompt,
        )
