from django.test import (
    SimpleTestCase,
)

from apps.ai_engine.services.web_evidence_quality_service import (
    WebEvidenceQualityService,
)


class WebEvidenceQualityServiceTests(SimpleTestCase):
    def test_requested_source_is_selected_semantically(
        self,
    ):
        result = WebEvidenceQualityService.select(
            results=[
                {
                    "title": ("Other " "publication"),
                    "url": ("https://" "other.example/" "story"),
                    "content": ("Fetched article " "about the public " "topic."),
                },
                {
                    "title": ("Example News - " "A Specific " "Public Article"),
                    "url": ("https://" "example-news.test/" "specific-article"),
                    "content": (
                        "Fetched publisher "
                        "page containing "
                        "the requested "
                        "article."
                    ),
                },
            ],
            search_plan={
                "query_type": "source_lookup",
                "freshness": "current",
                "resolved_question": ("Explain the " "requested article"),
                "requested_source": "Example News",
                "exact_title": "",
            },
            limit=5,
        )

        self.assertTrue(result["sufficient"])

        self.assertEqual(
            len(result["results"]),
            1,
        )

        self.assertIn(
            "example-news.test",
            result["results"][0]["url"],
        )

    def test_exact_article_lookup_fails_closed_on_title_mismatch(
        self,
    ):
        result = WebEvidenceQualityService.select(
            results=[
                {
                    "title": "Different article",
                    "url": ("https://" "example.test/" "different"),
                    "content": (
                        "Fetched content "
                        "exists but does not "
                        "match the requested "
                        "title."
                    ),
                }
            ],
            search_plan={
                "query_type": "article_lookup",
                "freshness": "recent",
                "resolved_question": ("Explain the " "requested article"),
                "requested_source": "",
                "exact_title": ("A Specific " "Public Article"),
            },
        )

        self.assertFalse(result["sufficient"])

        self.assertEqual(
            result["results"],
            [],
        )

    def test_search_snippet_without_fetched_page_is_not_evidence(
        self,
    ):
        result = WebEvidenceQualityService.select(
            results=[
                {
                    "title": "Search result only",
                    "url": ("https://" "example.test/" "story"),
                    "snippet": ("Search snippet " "without fetched " "page text."),
                    "content": "",
                }
            ],
            search_plan={
                "query_type": "current_fact",
                "freshness": "current",
                "resolved_question": ("Verify a " "current fact"),
                "requested_source": "",
                "exact_title": "",
            },
        )

        self.assertFalse(result["sufficient"])

        self.assertEqual(
            result["results"],
            [],
        )
