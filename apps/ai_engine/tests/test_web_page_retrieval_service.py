from unittest.mock import patch

from django.test import (
    SimpleTestCase,
)

from apps.ai_engine.services.web_page_retrieval_service import (
    WebPageRetrievalService,
)


class WebPageRetrievalServiceTests(SimpleTestCase):
    def test_private_ip_is_rejected(
        self,
    ):
        with patch(
            (
                "apps.ai_engine.services."
                "web_page_retrieval_service."
                "socket.getaddrinfo"
            ),
            return_value=[
                (
                    2,
                    1,
                    6,
                    "",
                    (
                        "127.0.0.1",
                        80,
                    ),
                )
            ],
        ):
            with self.assertRaises(ValueError):
                (
                    WebPageRetrievalService.validate_public_url(
                        ("http://example.test/" "private")
                    )
                )

    def test_article_text_and_publish_date_are_extracted(
        self,
    ):
        parsed = WebPageRetrievalService.extract_html(
            """
                <html>
                  <head>
                    <title>Current story</title>
                    <meta
                      property="article:published_time"
                      content="2026-10-05T10:00:00+05:30"
                    >
                  </head>

                  <body>
                    <nav>
                      This navigation text should
                      be ignored completely.
                    </nav>

                    <article>
                      <h1>
                        Current story
                      </h1>

                      <p>
                        This is a sufficiently long
                        paragraph containing the
                        verified current fact.
                      </p>

                      <p>
                        This is another sufficiently
                        long paragraph with supporting
                        details.
                      </p>
                    </article>
                  </body>
                </html>
                """
        )

        self.assertEqual(
            parsed["title"],
            "Current story",
        )

        self.assertIn(
            "verified current fact",
            parsed["content"],
        )

        self.assertNotIn(
            "navigation",
            parsed["content"],
        )

        self.assertEqual(
            parsed["published_at"],
            ("2026-10-05" "T10:00:00+05:30"),
        )
