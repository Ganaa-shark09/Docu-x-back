from django.test import SimpleTestCase

from apps.ai_engine.services.internal_document_relevance_service import (
    InternalDocumentRelevanceService,
)


class InternalDocumentRelevanceServiceTests(SimpleTestCase):
    def test_unrelated_short_message_does_not_use_hash_collision_chunk(self):
        chunks = [
            {
                "document_title": "Service event log",
                "document_filename": "events.pdf",
                "content": "ERROR Identity cache miss incident EVT-3020",
                "score": 0.31,
            }
        ]
        result = InternalDocumentRelevanceService.filter_relevant_chunks(
            question="hello",
            retrieved_chunks=chunks,
        )
        self.assertEqual(result, [])

    def test_real_lexical_document_match_is_kept(self):
        chunks = [
            {
                "document_title": "Leave Policy",
                "document_filename": "leave-policy.pdf",
                "content": "Employees receive annual leave according to grade.",
                "score": 0.8,
            }
        ]
        result = InternalDocumentRelevanceService.filter_relevant_chunks(
            question="What is the annual leave policy?",
            retrieved_chunks=chunks,
        )
        self.assertEqual(len(result), 1)
        self.assertIn(
            "leave",
            result[0]["metadata"]["internal_query_overlap_terms"],
        )

    def test_selected_document_scope_bypasses_lexical_gate(self):
        chunks = [
            {
                "document_title": "Quarterly Report",
                "content": "Revenue increased by 12 percent.",
            }
        ]
        result = InternalDocumentRelevanceService.filter_relevant_chunks(
            question="Explain this",
            retrieved_chunks=chunks,
            force_document_scope=True,
        )
        self.assertEqual(result, chunks)
