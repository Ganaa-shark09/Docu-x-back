from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engine.services.external_web_search_service import (
    ExternalWebSearchService,
)

from apps.ai_engine.services.web_search_fallback_provider_service import (
    BingRSSSearchProvider,
    DuckDuckGoLiteSearchProvider,
)


class WebSearchFallbackProviderTests(SimpleTestCase):
    def test_duckduckgo_lite_parser_extracts_result(
        self,
    ):
        value = """
        <html>
          <body>
            <a
              class="result-link"
              href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fstory"
            >
              Example Story
            </a>
            <td class="result-snippet">
              Example public search snippet.
            </td>
          </body>
        </html>
        """

        results = DuckDuckGoLiteSearchProvider._parse_html(
            value,
            max_results=5,
        )

        self.assertEqual(
            len(results),
            1,
        )

        self.assertEqual(
            results[0]["url"],
            ("https://" "example.com/" "story"),
        )

    def test_bing_rss_parser_extracts_result(
        self,
    ):
        value = """
        <rss version="2.0">
          <channel>
            <item>
              <title>Example Story</title>
              <link>https://example.com/story</link>
              <description>
                Example public search snippet.
              </description>
              <pubDate>
                Tue, 06 Oct 2026 08:00:00 GMT
              </pubDate>
            </item>
          </channel>
        </rss>
        """

        results = BingRSSSearchProvider._parse_rss(
            value,
            max_results=5,
        )

        self.assertEqual(
            len(results),
            1,
        )

        self.assertEqual(
            results[0]["source"],
            "bing_rss",
        )

    @patch(
        (
            "apps.ai_engine.services."
            "external_web_search_service."
            "BingRSSSearchProvider.search"
        )
    )
    @patch(
        (
            "apps.ai_engine.services."
            "external_web_search_service."
            "DuckDuckGoLiteSearchProvider.search"
        )
    )
    @patch(
        (
            "apps.ai_engine.services."
            "external_web_search_service."
            "DuckDuckGoSearchProvider.search_html"
        )
    )
    @patch.dict(
        "os.environ",
        {
            "WEB_SEARCH_PROVIDER": "auto",
            "SEARXNG_BASE_URL": "",
            "BRAVE_SEARCH_API_KEY": "",
        },
        clear=False,
    )
    def test_auto_falls_back_from_html_to_lite(
        self,
        html_search,
        lite_search,
        bing_search,
    ):
        html_search.return_value = {
            "results": [],
            "warnings": ["HTML blocked"],
        }

        lite_search.return_value = {
            "results": [
                {
                    "title": "Public Result",
                    "snippet": "Public snippet.",
                    "url": ("https://" "example.com/" "story"),
                    "source": "duckduckgo_lite",
                }
            ],
            "warnings": [],
        }

        response = ExternalWebSearchService._search_one(
            query="public query",
            max_results=5,
        )

        self.assertEqual(
            len(response["results"]),
            1,
        )

        lite_search.assert_called_once()

        self.assertTrue(response["results"])

    @patch(
        (
            "apps.ai_engine.services."
            "external_web_search_service."
            "BingRSSSearchProvider.search"
        )
    )
    @patch(
        (
            "apps.ai_engine.services."
            "external_web_search_service."
            "DuckDuckGoLiteSearchProvider.search"
        )
    )
    @patch(
        (
            "apps.ai_engine.services."
            "external_web_search_service."
            "DuckDuckGoSearchProvider.search_html"
        )
    )
    @patch.dict(
        "os.environ",
        {
            "WEB_SEARCH_PROVIDER": "auto",
            "SEARXNG_BASE_URL": "",
            "BRAVE_SEARCH_API_KEY": "",
        },
        clear=False,
    )
    def test_auto_falls_back_to_independent_rss_transport(
        self,
        html_search,
        lite_search,
        bing_search,
    ):
        html_search.return_value = {
            "results": [],
            "warnings": [],
        }

        lite_search.return_value = {
            "results": [],
            "warnings": [],
        }

        bing_search.return_value = {
            "results": [
                {
                    "title": "Public Result",
                    "snippet": "Public snippet.",
                    "url": ("https://" "example.com/" "story"),
                    "source": "bing_rss",
                }
            ],
            "warnings": [],
        }

        response = ExternalWebSearchService._search_one(
            query="public query",
            max_results=5,
        )

        self.assertEqual(
            len(response["results"]),
            1,
        )

        bing_search.assert_called_once()
