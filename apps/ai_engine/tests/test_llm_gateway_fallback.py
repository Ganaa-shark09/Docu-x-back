from django.test import SimpleTestCase

from apps.ai_engine.services.llm_gateway_service import LocalFallbackLLMProvider


class LocalFallbackLLMProviderTests(SimpleTestCase):
    def test_no_lexical_match_does_not_dump_arbitrary_chunk_content(self):
        marker = "SENSITIVE-RANDOM-SOURCE-CONTENT"
        result = LocalFallbackLLMProvider.generate_answer(
            question="hello",
            retrieved_chunks=[
                {
                    "content": marker,
                    "document_title": "Unrelated source",
                    "chunk_index": 0,
                }
            ],
        )
        self.assertNotIn(marker, result["answer"])
        self.assertIn("couldn't identify", result["answer"])
