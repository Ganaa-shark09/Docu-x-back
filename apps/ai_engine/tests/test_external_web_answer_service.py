from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engine.services.external_web_answer_service import (
    ExternalWebAnswerService,
)


class ExternalWebAnswerServiceTests(SimpleTestCase):
    def setUp(
        self,
    ):
        self.results = [
            {
                "title": "Current Story One",
                "url": ("https://" "news.example/" "one"),
                "published_at": ("2026-10-06T" "08:00:00Z"),
                "snippet": ("Short current " "summary one."),
                "content": ("Current public " "evidence one. " * 500),
                "content_source": "publisher_page",
            },
            {
                "title": "Current Story Two",
                "url": ("https://" "news.example/" "two"),
                "snippet": ("Short current " "summary two."),
                "content": ("Current public " "evidence two. " * 500),
                "content_source": "publisher_page",
            },
        ]

    @patch.dict(
        "os.environ",
        {
            "OLLAMA_WEB_ANSWER_MODEL": "fast-web-model",
            "OLLAMA_WEB_ANSWER_NUM_CTX": "4096",
            "OLLAMA_WEB_ANSWER_MAX_TOKENS": "450",
            "OLLAMA_WEB_ANSWER_TIMEOUT_SECONDS": "90",
            "EXTERNAL_WEB_ANSWER_MAX_RESULTS": "4",
            "EXTERNAL_WEB_ANSWER_PER_SOURCE_CHARS": "1200",
            "EXTERNAL_WEB_ANSWER_TOTAL_CHARS": "2400",
        },
        clear=False,
    )
    @patch(
        (
            "apps.ai_engine.services."
            "external_web_answer_service."
            "LLMGatewayService."
            "generate_answer"
        )
    )
    def test_web_answer_uses_bounded_fast_synthesis(
        self,
        generate_answer,
    ):
        generate_answer.return_value = {
            "answer": "Grounded answer.",
            "provider": "ollama",
            "model": "fast-web-model",
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "metadata": {},
        }

        response = ExternalWebAnswerService.generate(
            question="What happened today?",
            web_results=self.results,
            provider_config=object(),
        )

        self.assertEqual(
            response["answer"],
            "Grounded answer.",
        )

        kwargs = generate_answer.call_args.kwargs

        self.assertEqual(
            kwargs["generation_options"]["model"],
            "fast-web-model",
        )

        self.assertEqual(
            kwargs["generation_options"]["num_ctx"],
            4096,
        )

        self.assertEqual(
            kwargs["generation_options"]["max_tokens"],
            450,
        )

        self.assertLess(
            len(kwargs["prompt"]),
            10000,
        )

    @patch.dict(
        "os.environ",
        {
            "OLLAMA_WEB_ANSWER_MODEL": "fast-web-model",
            "OLLAMA_WEB_ANSWER_FALLBACK_MODEL": "fast-web-model",
        },
        clear=False,
    )
    @patch(
        (
            "apps.ai_engine.services."
            "external_web_answer_service."
            "LLMGatewayService."
            "generate_answer"
        )
    )
    def test_model_failure_returns_useful_web_evidence(
        self,
        generate_answer,
    ):
        generate_answer.return_value = {
            "answer": ("I couldn't complete " "the AI request."),
            "provider": "local_fallback",
            "model": "local-extractive-rag",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "metadata": {
                "fallback_reason": "ollama_request_failed",
                "ollama_model": "fast-web-model",
            },
        }

        response = ExternalWebAnswerService.generate(
            question="What happened today?",
            web_results=self.results,
            provider_config=object(),
        )

        self.assertEqual(
            response["provider"],
            "web_evidence_fallback",
        )

        self.assertIn(
            "Current Story One",
            response["answer"],
        )

        self.assertNotIn(
            ("I couldn't complete " "the AI request"),
            response["answer"],
        )

    def test_compaction_enforces_total_budget(
        self,
    ):
        with patch.dict(
            "os.environ",
            {
                "EXTERNAL_WEB_ANSWER_MAX_RESULTS": "4",
                "EXTERNAL_WEB_ANSWER_PER_SOURCE_CHARS": "1000",
                "EXTERNAL_WEB_ANSWER_TOTAL_CHARS": "1800",
            },
            clear=False,
        ):
            compact = ExternalWebAnswerService.compact_results(
                question="Current news?",
                web_results=self.results,
            )

        total = sum(len(item["evidence"]) for item in compact)

        self.assertLessEqual(
            total,
            1800,
        )


class ExternalWebAnswerDispatchTests(SimpleTestCase):
    def test_pure_web_turn_uses_dedicated_web_synthesis(
        self,
    ):
        self.assertTrue(
            ExternalWebAnswerService.should_handle(
                web_results=[
                    {
                        "title": "Public result",
                    }
                ],
                document_chunks=[],
            )
        )

    def test_document_plus_web_turn_keeps_combined_answer_path(
        self,
    ):
        self.assertFalse(
            ExternalWebAnswerService.should_handle(
                web_results=[
                    {
                        "title": "Public result",
                    }
                ],
                document_chunks=[
                    {
                        "content": "External document context",
                    }
                ],
            )
        )

    def test_no_web_evidence_does_not_use_web_synthesis(
        self,
    ):
        self.assertFalse(
            ExternalWebAnswerService.should_handle(
                web_results=[],
                document_chunks=[],
            )
        )
