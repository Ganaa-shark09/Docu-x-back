from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engine.services.external_conversation_summary_service import (
    ExternalConversationSummaryService,
)


class ExternalConversationSummaryServiceTests(SimpleTestCase):
    @patch(
        (
            "apps.ai_engine.services."
            "external_conversation_summary_service."
            "LLMGatewayService.generate_answer"
        )
    )
    @patch(
        (
            "apps.ai_engine.services."
            "external_conversation_summary_service."
            "LLMProviderConfigService."
            "get_default_config_for_company"
        )
    )
    @patch(
        (
            "apps.ai_engine.services."
            "external_conversation_summary_service."
            "ExternalConversationContextService."
            "build_history_messages"
        )
    )
    def test_summary_uses_broad_conversation_history(
        self,
        build_history,
        get_provider,
        generate_answer,
    ):
        build_history.return_value = [
            {
                "role": "user",
                "content": "Ask about current public news",
            },
            {
                "role": "assistant",
                "content": "Returned web-backed news",
            },
            {
                "role": "user",
                "content": "Ask a follow-up about another subject",
            },
            {
                "role": "assistant",
                "content": "Returned the follow-up answer",
            },
        ]

        get_provider.return_value = object()

        generate_answer.return_value = {
            "answer": ("The conversation covered " "public news and a follow-up."),
            "provider": "ollama",
            "model": "test-model",
            "prompt_tokens": 50,
            "completion_tokens": 20,
            "total_tokens": 70,
            "metadata": {},
        }

        result = ExternalConversationSummaryService.generate(
            user=SimpleNamespace(company=object()),
            conversation=object(),
            current_user_message_uuid="current",
            message="Summarize our chat",
        )

        self.assertIn(
            "public news",
            result["answer"],
        )

        kwargs = generate_answer.call_args.kwargs

        prompt = kwargs["prompt"]

        self.assertIn(
            "Ask about current public news",
            prompt,
        )

        self.assertIn(
            ("Ask a follow-up about " "another subject"),
            prompt,
        )

        self.assertEqual(
            build_history.call_args.kwargs["exclude_message_uuid"],
            "current",
        )

        self.assertEqual(
            build_history.call_args.kwargs["mode"],
            "summary",
        )
