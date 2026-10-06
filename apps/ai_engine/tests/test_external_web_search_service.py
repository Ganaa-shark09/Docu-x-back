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

        self.assertEqual(
            len(response["results"]),
            1,
        )
