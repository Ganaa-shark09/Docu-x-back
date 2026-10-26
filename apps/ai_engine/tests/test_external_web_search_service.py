from unittest.mock import (
    patch,
)

from django.test import (
    SimpleTestCase,
)

from apps.ai_engine.services.external_web_search_service import (
    ExternalWebSearchService,
)


class ExternalWebSearchServiceTests(SimpleTestCase):
    @patch(
        (
            "apps.ai_engine.services."
            "external_web_search_service."
            "WebPageRetrievalService."
            "enrich_results"
        )
    )
    @patch.object(
        ExternalWebSearchService,
        "_search_one",
    )
    def test_multiple_semantic_queries_are_executed_and_ranked(
        self,
        search_one,
        enrich_results,
    ):
        search_one.side_effect = [
            {
                "results": [
                    {
                        "title": ("Example News - " "A Specific " "Public Article"),
                        "url": ("https://" "example-news.test/" "specific-article"),
                        "snippet": "First discovery",
                        "source": "test",
                    }
                ],
                "warnings": [],
            },
            {
                "results": [
                    {
                        "title": ("Broader " "related result"),
                        "url": ("https://" "other.example/" "related"),
                        "snippet": "Second discovery",
                        "source": "test",
                    }
                ],
                "warnings": [],
            },
        ]

        def enrich(
            items,
            max_results,
        ):
            return {
                "results": [
                    {
                        **item,
                        "content": (
                            "Fetched publisher " "content used for " "grounding."
                        ),
                    }
                    for item in items
                ],
                "warnings": [],
            }

        enrich_results.side_effect = enrich

        response = ExternalWebSearchService.search(
            fallback_query="fallback query",
            search_plan={
                "needs_web": True,
                "freshness": "current",
                "query_type": "article_lookup",
                "resolved_question": ("Explain the " "requested article"),
                "requested_source": "Example News",
                "exact_title": ("A Specific " "Public Article"),
                "search_queries": [
                    ('"A Specific ' 'Public Article" ' '"Example News"'),
                    ("A Specific " "Public Article " "Example News"),
                ],
            },
            max_results=5,
        )

        self.assertEqual(
            search_one.call_count,
            2,
        )

        self.assertTrue(response["sufficient"])

        self.assertEqual(
            len(response["results"]),
            1,
        )

        self.assertIn(
            "example-news.test",
            response["results"][0]["url"],
        )


class ExternalWebSearchProviderFallbackTests(SimpleTestCase):
    @patch.dict(
        "os.environ",
        {
            "WEB_SEARCH_PROVIDER": "auto",
            "SEARXNG_BASE_URL": "",
            "BRAVE_SEARCH_API_KEY": "",
        },
        clear=False,
    )
    @patch(
        (
            "apps.ai_engine.services."
            "external_web_search_service."
            "DuckDuckGoSearchProvider."
            "search_html"
        )
    )
    def test_auto_provider_uses_duckduckgo_discovery_fallback(
        self,
        search_html,
    ):
        search_html.return_value = {
            "results": [
                {
                    "title": "Public result",
                    "snippet": "Public snippet",
                    "url": ("https://" "example.test/" "story"),
                    "source": "duckduckgo_html",
                }
            ],
            "warnings": [],
        }

        response = ExternalWebSearchService._search_one(
            query="public query",
            max_results=5,
        )

        search_html.assert_called_once()

        self.assertGreaterEqual(
            len(response["results"]),
            1,
        )


class ExternalWebSearchEvidenceFallbackTests(SimpleTestCase):
    @patch(
        (
            "apps.ai_engine.services."
            "external_web_search_service."
            "WebPageRetrievalService."
            "enrich_results"
        )
    )
    @patch.object(
        ExternalWebSearchService,
        "_search_one",
    )
    def test_news_uses_search_evidence_when_page_fetch_fails(
        self,
        search_one,
        enrich_results,
    ):
        search_one.return_value = {
            "results": [
                {
                    "title": "Current Headline One",
                    "snippet": ("Current news " "summary one."),
                    "url": ("https://" "news.example/" "one"),
                    "source": "search",
                },
                {
                    "title": "Current Headline Two",
                    "snippet": ("Current news " "summary two."),
                    "url": ("https://" "news.example/" "two"),
                    "source": "search",
                },
            ],
            "warnings": [],
        }

        enrich_results.return_value = {
            "results": [
                {
                    "title": "Current Headline One",
                    "snippet": ("Current news " "summary one."),
                    "url": ("https://" "news.example/" "one"),
                    "content": "",
                },
                {
                    "title": "Current Headline Two",
                    "snippet": ("Current news " "summary two."),
                    "url": ("https://" "news.example/" "two"),
                    "content": "",
                },
            ],
            "warnings": [("Publisher pages " "could not be read.")],
        }

        response = ExternalWebSearchService.search(
            fallback_query="today news",
            search_plan={
                "needs_web": True,
                "freshness": "current",
                "query_type": "news",
                "resolved_question": "today news",
                "requested_source": "",
                "exact_title": "",
                "search_queries": ["today news"],
            },
            max_results=5,
        )

        self.assertTrue(response["sufficient"])

        self.assertEqual(
            response["evidence_mode"],
            "search_discovery",
        )

        self.assertEqual(
            response["warnings"],
            [],
        )

        self.assertTrue(
            response["diagnostics"],
        )
