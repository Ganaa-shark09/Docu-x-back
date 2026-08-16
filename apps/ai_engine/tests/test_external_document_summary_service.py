import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engine.services.external_document_summary_service import (
    ExternalDocumentSummaryService,
)


class ExternalDocumentSummaryServiceTests(SimpleTestCase):
    def test_short_text_is_single_summary_part(self):
        text = "Section one.\n\nSection two."

        chunks = ExternalDocumentSummaryService.split_for_summary(text)

        self.assertEqual(chunks, [text])

    def test_long_text_keeps_start_and_end_content(self):
        text = "\n\n".join(
            f"Section {index}: " + (f"content-{index} " * 30) for index in range(1, 40)
        )

        chunks = ExternalDocumentSummaryService.split_for_summary(text)

        self.assertGreater(len(chunks), 1)
        self.assertIn("Section 1:", chunks[0])
        self.assertIn("Section 39:", chunks[-1])

    def test_small_document_can_use_single_pass(self):
        text = "Short factual section. " * 80

        with patch.dict(
            os.environ,
            {
                "EXTERNAL_AI_SUMMARY_SINGLE_PASS_CHAR_LIMIT": "7000",
                "EXTERNAL_AI_SUMMARY_SINGLE_PASS_SOURCE_TOKEN_LIMIT": "1400",
                "OLLAMA_DOCUMENT_SUMMARY_NUM_CTX": "4096",
                "OLLAMA_DOCUMENT_SUMMARY_MAX_TOKENS": "900",
                "EXTERNAL_AI_SUMMARY_PROMPT_OVERHEAD_TOKENS": "650",
                "EXTERNAL_AI_SUMMARY_CONTEXT_SAFETY_RATIO": "0.90",
            },
            clear=False,
        ):
            self.assertTrue(
                ExternalDocumentSummaryService.should_use_single_pass(
                    text,
                    "summarize this file",
                )
            )

    def test_medium_document_uses_hierarchical_cpu_budget(self):
        text = (
            "Entity | Region | Score | Status\n" + "ENT-001 | Europe | 47 | Moderate\n"
        ) * 180

        with patch.dict(
            os.environ,
            {
                "EXTERNAL_AI_SUMMARY_SINGLE_PASS_CHAR_LIMIT": "7000",
                "EXTERNAL_AI_SUMMARY_SINGLE_PASS_SOURCE_TOKEN_LIMIT": "1400",
                "OLLAMA_DOCUMENT_SUMMARY_NUM_CTX": "4096",
                "OLLAMA_DOCUMENT_SUMMARY_MAX_TOKENS": "900",
                "EXTERNAL_AI_SUMMARY_PROMPT_OVERHEAD_TOKENS": "650",
                "EXTERNAL_AI_SUMMARY_CONTEXT_SAFETY_RATIO": "0.90",
            },
            clear=False,
        ):
            self.assertFalse(
                ExternalDocumentSummaryService.should_use_single_pass(
                    text,
                    "summarize this file and explain everything",
                )
            )

    def test_large_document_uses_hierarchical_summary(self):
        text = "Long source section with identifiers and values. " * 900

        with patch.dict(
            os.environ,
            {
                "EXTERNAL_AI_SUMMARY_SINGLE_PASS_CHAR_LIMIT": "18000",
                "OLLAMA_DOCUMENT_SUMMARY_NUM_CTX": "8192",
                "OLLAMA_DOCUMENT_SUMMARY_MAX_TOKENS": "1200",
                "EXTERNAL_AI_SUMMARY_PROMPT_OVERHEAD_TOKENS": "650",
                "EXTERNAL_AI_SUMMARY_CONTEXT_SAFETY_RATIO": "0.90",
            },
            clear=False,
        ):
            self.assertFalse(
                ExternalDocumentSummaryService.should_use_single_pass(
                    text,
                    "summarize the whole document",
                )
            )

    def test_retry_split_produces_smaller_parts_without_losing_ends(self):
        text = "\n\n".join(
            f"Section {index}: " + ("detail " * 80) for index in range(1, 10)
        )

        with patch.dict(
            os.environ,
            {"EXTERNAL_AI_SUMMARY_RETRY_MIN_CHARS": "1000"},
            clear=False,
        ):
            chunks = ExternalDocumentSummaryService.split_text_for_retry(text)

        self.assertGreater(len(chunks), 1)
        self.assertIn("Section 1:", chunks[0])
        self.assertIn("Section 9:", chunks[-1])

    def test_timeout_failure_is_retryable_but_connection_failure_is_not(self):
        self.assertTrue(
            ExternalDocumentSummaryService.is_retryable_map_failure(
                {
                    "fallback_error_type": "ReadTimeout",
                    "fallback_error": "HTTPConnectionPool timed out",
                }
            )
        )
        self.assertFalse(
            ExternalDocumentSummaryService.is_retryable_map_failure(
                {
                    "fallback_error_type": "ConnectionError",
                    "fallback_error": "Connection refused",
                }
            )
        )

    def test_coverage_cache_reuses_only_matching_source_and_model(self):
        class FakeExtraction:
            def __init__(self):
                self.metadata = {}
                self.saved_update_fields = None

            def save(self, update_fields=None):
                self.saved_update_fields = update_fields

        extraction = FakeExtraction()
        document = SimpleNamespace(text_extraction=extraction)
        source = "Section A\nImportant value: 42"

        with patch.dict(
            os.environ,
            {
                "OLLAMA_DOCUMENT_SUMMARY_MAP_MODEL": "qwen3:4b-instruct",
                "EXTERNAL_AI_SUMMARY_CHUNK_CHAR_LIMIT": "4200",
                "EXTERNAL_AI_SUMMARY_CHUNK_OVERLAP": "150",
            },
            clear=False,
        ):
            ExternalDocumentSummaryService.save_coverage_cache(
                document=document,
                full_text=source,
                notes=["[Coverage part 1/1]\nImportant value: 42"],
                initial_parts=1,
                effective_parts=1,
            )

            cached = ExternalDocumentSummaryService.load_coverage_cache(
                document,
                source,
            )
            stale = ExternalDocumentSummaryService.load_coverage_cache(
                document,
                source + " changed",
            )

        self.assertIsNotNone(cached)
        self.assertEqual(cached["effective_parts"], 1)
        self.assertIsNone(stale)
        self.assertIn("metadata", extraction.saved_update_fields)
