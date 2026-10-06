import html
import os
import re
import xml.etree.ElementTree as ET

from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit

import requests


class _DuckDuckGoLiteParser(HTMLParser):
    def __init__(self):
        super().__init__()

        self.results = []
        self.current = None

        self.capture_title = False
        self.capture_snippet = False
        self.snippet_tag = None

    def handle_starttag(
        self,
        tag,
        attrs,
    ):
        attrs = dict(attrs)

        classes = set(str(attrs.get("class") or "").split())

        if tag == "a" and "result-link" in classes:
            self._finish_current()

            self.current = {
                "url": attrs.get("href") or "",
                "title_parts": [],
                "snippet_parts": [],
            }

            self.capture_title = True
            self.capture_snippet = False
            self.snippet_tag = None

            return

        if self.current and "result-snippet" in classes:
            self.capture_snippet = True
            self.snippet_tag = tag

    def handle_endtag(
        self,
        tag,
    ):
        if tag == "a" and self.capture_title:
            self.capture_title = False

        if self.capture_snippet and tag == self.snippet_tag:
            self.capture_snippet = False
            self.snippet_tag = None

            self._finish_current()

    def handle_data(
        self,
        data,
    ):
        if not self.current:
            return

        if self.capture_title:
            self.current["title_parts"].append(data)

        if self.capture_snippet:
            self.current["snippet_parts"].append(data)

    def close(self):
        super().close()
        self._finish_current()

    def _finish_current(
        self,
    ):
        if not self.current:
            return

        title = " ".join("".join(self.current["title_parts"]).split())

        snippet = " ".join("".join(self.current["snippet_parts"]).split())

        url = str(self.current.get("url") or "").strip()

        if title and snippet and url:
            self.results.append(
                {
                    "title": html.unescape(title),
                    "snippet": html.unescape(snippet),
                    "url": url,
                }
            )

        self.current = None
        self.capture_title = False
        self.capture_snippet = False
        self.snippet_tag = None


class DuckDuckGoLiteSearchProvider:
    ENDPOINT = "https://lite.duckduckgo.com/lite/"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": (
            "text/html," "application/xhtml+xml," "application/xml;q=0.9," "*/*;q=0.8"
        ),
        "Referer": "https://lite.duckduckgo.com/",
    }

    @classmethod
    def search(
        cls,
        query,
        max_results=10,
    ):
        timeout = int(
            os.getenv(
                "WEB_SEARCH_TIMEOUT_SECONDS",
                "10",
            )
        )

        warnings = []

        attempts = [
            "get",
            "post",
        ]

        for method in attempts:
            try:
                if method == "get":
                    response = requests.get(
                        cls.ENDPOINT,
                        params={
                            "q": query,
                        },
                        headers=cls.HEADERS,
                        timeout=timeout,
                    )

                else:
                    response = requests.post(
                        cls.ENDPOINT,
                        data={
                            "q": query,
                        },
                        headers=cls.HEADERS,
                        timeout=timeout,
                    )

                if response.status_code in {
                    403,
                    429,
                }:
                    warnings.append(
                        (
                            "DuckDuckGo Lite "
                            f"{method.upper()} "
                            "was rejected with "
                            f"HTTP "
                            f"{response.status_code}."
                        )
                    )

                    continue

                response.raise_for_status()

                results = cls._parse_html(
                    response.text,
                    max_results=max_results,
                )

                if results:
                    return {
                        "results": results,
                        "warnings": warnings,
                    }

                warnings.append(
                    (
                        "DuckDuckGo Lite "
                        f"{method.upper()} "
                        "returned no parseable "
                        "results."
                    )
                )

            except Exception as exc:
                warnings.append(
                    ("DuckDuckGo Lite " f"{method.upper()} " "unavailable: " f"{exc}")
                )

        return {
            "results": [],
            "warnings": warnings,
        }

    @classmethod
    def _parse_html(
        cls,
        value,
        max_results=10,
    ):
        parser = _DuckDuckGoLiteParser()

        parser.feed(value or "")

        parser.close()

        output = []
        seen = set()

        for raw in parser.results:
            url = cls._clean_url(raw.get("url"))

            title = str(raw.get("title") or "").strip()

            snippet = str(raw.get("snippet") or "").strip()

            if not url or not title or not snippet:
                continue

            if url in seen:
                continue

            seen.add(url)

            output.append(
                {
                    "title": title,
                    "snippet": snippet,
                    "url": url,
                    "source": "duckduckgo_lite",
                }
            )

            if len(output) >= max_results:
                break

        return output

    @staticmethod
    def _clean_url(
        value,
    ):
        value = html.unescape(str(value or "").strip())

        if value.startswith("//"):
            value = "https:" + value

        parsed = urlsplit(value)

        hostname = (parsed.hostname or "").casefold()

        if hostname.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
            target = parse_qs(parsed.query).get("uddg") or []

            if target:
                return unquote(target[0])

        return value


class BingRSSSearchProvider:
    ENDPOINT = "https://www.bing.com/search"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml,"
        "application/xml,"
        "text/xml;q=0.9,"
        "*/*;q=0.8",
    }

    @classmethod
    def search(
        cls,
        query,
        max_results=10,
    ):
        try:
            response = requests.get(
                cls.ENDPOINT,
                params={
                    "q": query,
                    "format": "rss",
                    "count": max_results,
                },
                headers=cls.HEADERS,
                timeout=int(
                    os.getenv(
                        "WEB_SEARCH_TIMEOUT_SECONDS",
                        "10",
                    )
                ),
            )

            response.raise_for_status()

            results = cls._parse_rss(
                response.text,
                max_results=max_results,
            )

            if not results:
                return {
                    "results": [],
                    "warnings": [
                        ("Bing RSS search " "returned no " "parseable results.")
                    ],
                }

            return {
                "results": results,
                "warnings": [],
            }

        except Exception as exc:
            return {
                "results": [],
                "warnings": [("Bing RSS search " f"unavailable: {exc}")],
            }

    @classmethod
    def _parse_rss(
        cls,
        value,
        max_results=10,
    ):
        try:
            root = ET.fromstring(value or "")

        except ET.ParseError:
            return []

        output = []
        seen = set()

        for item in root.findall(".//item"):
            title = cls._clean_text(item.findtext("title"))

            snippet = cls._clean_text(item.findtext("description"))

            url = str(item.findtext("link") or "").strip()

            published_at = str(item.findtext("pubDate") or "").strip()

            parsed = urlsplit(url)

            if (
                parsed.scheme.lower()
                not in {
                    "http",
                    "https",
                }
                or not parsed.netloc
            ):
                continue

            if not title or not snippet:
                continue

            if url in seen:
                continue

            seen.add(url)

            output.append(
                {
                    "title": title,
                    "snippet": snippet,
                    "url": url,
                    "published_at": published_at,
                    "source": "bing_rss",
                }
            )

            if len(output) >= max_results:
                break

        return output

    @staticmethod
    def _clean_text(
        value,
    ):
        value = html.unescape(str(value or ""))

        value = re.sub(
            r"<[^>]+>",
            " ",
            value,
        )

        return " ".join(value.split())
