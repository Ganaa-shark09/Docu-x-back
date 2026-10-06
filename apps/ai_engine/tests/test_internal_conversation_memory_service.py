from django.test import SimpleTestCase

from apps.ai_engine.services.internal_conversation_memory_service import (
    InternalConversationMemoryService,
)


class InternalConversationMemoryServiceTests(SimpleTestCase):
    def test_detects_chat_summary_request(
        self,
    ):
        requests = [
            "Please summarize our chat",
            "Recap this conversation",
            "What have we discussed?",
            "Catch me up",
        ]

        for message in requests:
            with self.subTest(message=message):
                self.assertTrue(
                    InternalConversationMemoryService.is_summary_request(message)
                )

    def test_document_summary_is_not_chat_summary(
        self,
    ):
        self.assertFalse(
            InternalConversationMemoryService.is_summary_request(
                "Summarize this contract"
            )
        )

    def test_accessible_document_answer_remains_available(
        self,
    ):
        rows = [
            {
                "role": "assistant",
                "content": "The contract expires in June.",
                "retrieved_sources": [
                    {
                        "source_type": "internal_document",
                        "document_uuid": "doc-1",
                    }
                ],
                "metadata": {},
            }
        ]

        result = InternalConversationMemoryService.prepare_rows(
            rows=rows,
            accessible_document_uuids={"doc-1"},
            mode="answer",
        )

        self.assertEqual(
            result[0]["content"],
            ("The contract " "expires in June."),
        )

    def test_revoked_document_answer_is_redacted_from_memory(
        self,
    ):
        rows = [
            {
                "role": "assistant",
                "content": ("Confidential payroll " "information."),
                "retrieved_sources": [
                    {
                        "source_type": "internal_document",
                        "document_uuid": "payroll-doc",
                    }
                ],
                "metadata": {},
            }
        ]

        result = InternalConversationMemoryService.prepare_rows(
            rows=rows,
            accessible_document_uuids=set(),
            mode="summary",
        )

        self.assertEqual(
            result[0]["content"],
            (InternalConversationMemoryService.REVOKED_CONTEXT_MESSAGE),
        )

        self.assertNotIn(
            "payroll",
            result[0]["content"].lower(),
        )

    def test_short_followup_resolves_against_previous_user_topic(
        self,
    ):
        resolved = InternalConversationMemoryService.resolve_retrieval_query(
            message="What about that?",
            history=[
                {
                    "role": "user",
                    "content": (
                        "Explain the termination "
                        "clause in the supplier "
                        "agreement."
                    ),
                },
                {
                    "role": "assistant",
                    "content": "Earlier answer.",
                },
            ],
        )

        self.assertIn(
            ("termination clause " "in the supplier agreement"),
            resolved,
        )

        self.assertIn(
            "What about that?",
            resolved,
        )

    def test_standalone_question_is_not_rewritten(
        self,
    ):
        question = "What is the retention period " "for approved invoices?"

        resolved = InternalConversationMemoryService.resolve_retrieval_query(
            message=question,
            history=[
                {
                    "role": "user",
                    "content": "Earlier unrelated question",
                }
            ],
        )

        self.assertEqual(
            resolved,
            question,
        )
