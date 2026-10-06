from django.test import SimpleTestCase

from apps.ai_engine.services.external_conversation_context_service import (
    ExternalConversationContextService,
)


class ExternalConversationContextServiceTests(SimpleTestCase):
    def test_detects_conversation_summary_request(
        self,
    ):
        requests = [
            "Please summarize our chat",
            "Can you recap this conversation?",
            "What have we discussed so far?",
            "Catch me up on our conversation",
        ]

        for message in requests:
            with self.subTest(message=message):
                self.assertTrue(
                    ExternalConversationContextService.is_summary_request(message)
                )

    def test_does_not_confuse_document_summary_with_chat_summary(
        self,
    ):
        self.assertFalse(
            ExternalConversationContextService.is_summary_request(
                "Summarize this uploaded contract"
            )
        )

        self.assertFalse(
            ExternalConversationContextService.is_summary_request(
                "Summarize this news article"
            )
        )

    def test_summary_mode_keeps_broad_conversation_context(
        self,
    ):
        rows = []

        for index in range(20):
            rows.append(
                {
                    "role": ("user" if index % 2 == 0 else "assistant"),
                    "content": (f"Conversation message " f"{index}"),
                    "metadata": {},
                }
            )

        result = ExternalConversationContextService.bound_history_rows(
            rows=rows,
            mode="summary",
        )

        self.assertEqual(
            len(result),
            20,
        )

        self.assertIn(
            "message 0",
            result[0]["content"],
        )

        self.assertIn(
            "message 19",
            result[-1]["content"],
        )

    def test_routing_memory_ignores_failed_clarification_answer(
        self,
    ):
        result = ExternalConversationContextService.bound_history_rows(
            rows=[
                {
                    "role": "user",
                    "content": "Tell me about the subject",
                    "metadata": {},
                },
                {
                    "role": "assistant",
                    "content": "Please attach a document",
                    "metadata": {"semantic_action": "clarify"},
                },
                {
                    "role": "user",
                    "content": "What about the background?",
                    "metadata": {},
                },
            ],
            mode="routing",
        )

        contents = [item["content"] for item in result]

        self.assertNotIn(
            "Please attach a document",
            contents,
        )

        self.assertIn(
            "What about the background?",
            contents,
        )
