from django.test import (
    SimpleTestCase,
)

from apps.ai_engine.services.web_search_provider_service import (
    DuckDuckGoSearchProvider,
    WebSearchProviderService,
)


class WebSearchProviderServiceTests(SimpleTestCase):
    def test_duckduckgo_redirect_is_unwrapped(
        self,
    ):
        url = (
            "https://duckduckgo.com/l/"
            "?uddg="
            "https%3A%2F%2F"
            "example-news.test"
            "%2Fpublic-story"
        )

        self.assertEqual(
            DuckDuckGoSearchProvider.clean_url(url),
            ("https://" "example-news.test/" "public-story"),
        )

    def test_explicit_domain_query_is_ranked_generically(
        self,
    ):
        results = [
            {
                "title": "Other result",
                "snippet": ("Public story " "summary"),
                "url": ("https://" "other.example/" "story"),
                "source": "test",
            },
            {
                "title": ("Requested " "public story"),
                "snippet": "Public story",
                "url": ("https://" "example-news.test/" "story"),
                "source": "test",
            },
        ]

        ranked = WebSearchProviderService.rank_results(
            ("public story " "site:" "example-news.test"),
            results,
        )

        self.assertEqual(
            ranked[0]["url"],
            ("https://" "example-news.test/" "story"),
        )
