from django.test import SimpleTestCase

from apps.ai_engine.services.web_discovery_evidence_service import (
    WebDiscoveryEvidenceService,
)


class WebDiscoveryEvidenceServiceTests(SimpleTestCase):
    def test_news_uses_search_discovery_when_page_fetch_is_unavailable(
        self,
    ):
        response = WebDiscoveryEvidenceService.select(
            discovery_results=[
                {
                    "title": "Current Headline One",
                    "snippet": ("A current public " "news summary."),
                    "url": ("https://" "publisher.example/" "story-one"),
                },
                {
                    "title": "Current Headline Two",
                    "snippet": ("Another current " "news summary."),
                    "url": ("https://" "publisher.example/" "story-two"),
                },
            ],
            search_plan={
                "query_type": "news",
                "freshness": "current",
                "resolved_question": "today's news",
                "requested_source": "",
                "exact_title": "",
            },
            limit=5,
        )

        self.assertTrue(response["sufficient"])

        self.assertEqual(
            response["evidence_mode"],
            "search_discovery",
        )

        self.assertEqual(
            response["results"][0]["content_source"],
            ("search_result_" "snippet"),
        )

    def test_current_fact_needs_multiple_hosts_for_snippet_only_evidence(
        self,
    ):
        response = WebDiscoveryEvidenceService.select(
            discovery_results=[
                {
                    "title": "Source One",
                    "snippet": ("Current claim " "reported here."),
                    "url": ("https://" "one.example/" "story"),
                },
                {
                    "title": "Source Two",
                    "snippet": ("Current claim " "also reported here."),
                    "url": ("https://" "two.example/" "story"),
                },
            ],
            search_plan={
                "query_type": "current_fact",
                "freshness": "current",
                "resolved_question": ("Verify a current " "public claim"),
                "requested_source": "",
                "exact_title": "",
            },
        )

        self.assertTrue(response["sufficient"])

    def test_exact_article_still_requires_page_level_evidence(
        self,
    ):
        response = WebDiscoveryEvidenceService.select(
            discovery_results=[
                {
                    "title": "Specific Article",
                    "snippet": "A search snippet.",
                    "url": ("https://" "publisher.example/" "article"),
                }
            ],
            search_plan={
                "query_type": "article_lookup",
                "freshness": "recent",
                "resolved_question": "Explain this article",
                "requested_source": "",
                "exact_title": "Specific Article",
            },
        )

        self.assertFalse(response["sufficient"])
