import html
import os
import re

from html.parser import HTMLParser
from urllib.parse import (
    parse_qs,
    quote,
    unquote,
    urlsplit,
)

import requests

from apps.ai_engine.services.web_page_retrieval_service import (
    WebPageRetrievalService,
)


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(
            convert_charrefs=True,
        )

        self.current_kind = None
        self.current_href = ""
        self.current_parts = []

        self.results = []
        self.pending = None

    def handle_starttag(
        self,
        tag,
        attrs,
    ):
        if tag.lower() != "a":
            return

        attrs_dict = {str(key).lower(): value for key, value in attrs}

        classes = set(str(attrs_dict.get("class") or "").split())

        if "result__a" in classes:
            self.current_kind = "title"

            self.current_href = str(attrs_dict.get("href") or "")

            self.current_parts = []

        elif "result__snippet" in classes:
            self.current_kind = "snippet"

            self.current_href = ""
            self.current_parts = []

    def handle_endtag(
        self,
        tag,
    ):
        if tag.lower() != "a" or not self.current_kind:
            return

        value = re.sub(
            r"\s+",
            " ",
            " ".join(self.current_parts),
        ).strip()

        if self.current_kind == "title":
            self.pending = {
                "url": self.current_href,
                "title": value,
                "snippet": "",
            }

        elif self.current_kind == "snippet" and self.pending:
            self.pending["snippet"] = value

            self.results.append(self.pending)

            self.pending = None

        self.current_kind = None
        self.current_href = ""
        self.current_parts = []

    def handle_data(
        self,
        data,
    ):
        if not self.current_kind:
            return

        value = str(data or "").strip()

        if value:
            self.current_parts.append(value)


class WebSearchProviderService:
    STOPWORDS = {
        "who",
        "what",
        "when",
        "where",
        "why",
        "how",
        "is",
        "are",
        "the",
        "a",
        "an",
        "of",
        "in",
        "to",
        "for",
        "current",
        "currently",
        "latest",
        "today",
        "now",
        "tell",
        "me",
        "about",
        "please",
        "can",
        "you",
        "site",
    }

    FRESH_RE = re.compile(
        r"\b(?:"
        r"today|latest|current|currently|"
        r"now|breaking|recent|live|news|"
        r"this week|this month|as of"
        r")\b",
        re.I,
    )

    @classmethod
    def search(
        cls,
        query,
        max_results=5,
    ):
        return cls.search_with_metadata(
            query=query,
            max_results=max_results,
        )["results"]

    @classmethod
    def search_with_metadata(
        cls,
        query,
        max_results=5,
    ):
        query = re.sub(
            r"\s+",
            " ",
            str(query or ""),
        ).strip()

        if not query:
            return {
                "results": [],
                "warnings": ["Web search query was empty."],
            }

        provider = (
            os.getenv(
                "WEB_SEARCH_PROVIDER",
                "auto",
            )
            .strip()
            .lower()
        )

        if provider == "disabled":
            return {
                "results": [],
                "warnings": ["Web search provider is disabled."],
            }

        search_limit = max(
            max_results * 2,
            8,
        )

        all_results = []
        warnings = []

        searxng_base_url = os.getenv(
            "SEARXNG_BASE_URL",
            "",
        ).strip()

        brave_key = os.getenv(
            "BRAVE_SEARCH_API_KEY",
            "",
        ).strip()

        if (
            provider
            in {
                "searxng",
                "auto",
            }
            and searxng_base_url
        ):
            response = SearXNGSearchProvider.search(
                query=query,
                max_results=search_limit,
                base_url=searxng_base_url,
            )

            all_results.extend(response["results"])

            warnings.extend(response["warnings"])

        elif provider == "searxng":
            warnings.append("SEARXNG_BASE_URL " "is not configured.")

        if (
            provider
            in {
                "brave",
                "auto",
            }
            and brave_key
        ):
            response = BraveSearchProvider.search(
                query=query,
                max_results=search_limit,
                api_key=brave_key,
            )

            all_results.extend(response["results"])

            warnings.extend(response["warnings"])

        elif provider == "brave":
            warnings.append("BRAVE_SEARCH_API_KEY " "is not configured.")

        if provider == "duckduckgo" or (
            provider == "auto" and len(all_results) < max_results
        ):
            response = DuckDuckGoSearchProvider.search(
                query=query,
                max_results=search_limit,
            )

            all_results.extend(response["results"])

            warnings.extend(response["warnings"])

        # Wikipedia is only a stable-knowledge fallback.
        # Never let it displace current publisher evidence.
        if not cls.is_fresh_query(query) and len(all_results) < max_results:
            response = WikipediaSearchProvider.search(
                query=query,
                max_results=2,
            )

            all_results.extend(response["results"])

            warnings.extend(response["warnings"])

        results = cls.dedupe_results(all_results)

        results = cls.filter_low_quality_results(
            query,
            results,
        )

        results = cls.rank_results(
            query,
            results,
        )[:max_results]

        fetch_count = min(
            max_results,
            int(
                os.getenv(
                    "WEB_PAGE_FETCH_MAX_RESULTS",
                    "4",
                )
            ),
        )

        enriched = WebPageRetrievalService.enrich_results(
            results,
            max_results=fetch_count,
        )

        warnings.extend(enriched["warnings"])

        return {
            "results": enriched["results"][:max_results],
            "warnings": cls.dedupe_strings(warnings),
        }

    @classmethod
    def is_fresh_query(
        cls,
        query,
    ):
        return bool(cls.FRESH_RE.search(str(query or "")))

    @classmethod
    def filter_low_quality_results(
        cls,
        query,
        results,
    ):
        query_terms = cls.query_terms(query)

        requested_domain = cls.requested_domain(query)

        if not query_terms and not requested_domain:
            return results

        filtered = []

        for result in results:
            title = str(result.get("title") or "")

            snippet = str(result.get("snippet") or "")

            combined = f"{title} {snippet}".lower()

            url = str(result.get("url") or "")

            domain = (urlsplit(url).hostname or "").lower()

            domain_match = bool(
                requested_domain
                and (
                    domain == requested_domain
                    or domain.endswith(f".{requested_domain}")
                )
            )

            matched = [term for term in query_terms if term in combined]

            if domain_match or matched:
                filtered.append(result)

        return filtered

    @classmethod
    def rank_results(
        cls,
        query,
        results,
    ):
        query_terms = cls.query_terms(query)

        requested_domain = cls.requested_domain(query)

        fresh = cls.is_fresh_query(query)

        scored = []

        for (
            index,
            result,
        ) in enumerate(results):
            title = str(result.get("title") or "").lower()

            snippet = str(result.get("snippet") or "").lower()

            url = str(result.get("url") or "")

            domain = (urlsplit(url).hostname or "").lower()

            score = 0

            for term in query_terms:
                if term in title:
                    score += 5

                elif term in snippet:
                    score += 2

            if requested_domain and (
                domain == requested_domain or domain.endswith(f".{requested_domain}")
            ):
                score += 100

            if fresh and result.get("source") == "wikipedia":
                score -= 50

            if domain.endswith(".gov.in") or domain.endswith(".nic.in"):
                score += 8

            scored.append(
                (
                    score,
                    -index,
                    result,
                )
            )

        scored.sort(
            key=lambda item: (
                item[0],
                item[1],
            ),
            reverse=True,
        )

        return [item[2] for item in scored]

    @classmethod
    def query_terms(
        cls,
        query,
    ):
        return {
            term
            for term in re.findall(
                r"[a-zA-Z0-9]+",
                str(query or "").lower(),
            )
            if (len(term) > 2 and term not in cls.STOPWORDS)
        }

    @staticmethod
    def requested_domain(
        query,
    ):
        match = re.search(
            r"\bsite:([a-z0-9.-]+)",
            str(query or "").lower(),
        )

        return match.group(1).strip(".") if match else ""

    @staticmethod
    def dedupe_results(
        results,
    ):
        seen = set()
        output = []

        for result in results:
            item = dict(result or {})

            url = DuckDuckGoSearchProvider.clean_url(item.get("url") or "")

            item["url"] = url

            title = str(item.get("title") or "").strip()

            parsed = urlsplit(url)

            normalized_url = (
                (
                    f"{parsed.scheme.lower()}"
                    f"://"
                    f"{parsed.netloc.lower()}"
                    f"{parsed.path.rstrip('/')}"
                )
                if (parsed.scheme and parsed.netloc)
                else url
            )

            key = normalized_url or title.lower()

            if not key or key in seen:
                continue

            seen.add(key)

            output.append(item)

        return output

    @staticmethod
    def dedupe_strings(
        values,
    ):
        seen = set()
        output = []

        for value in values:
            text = str(value or "").strip()

            if not text or text in seen:
                continue

            seen.add(text)

            output.append(text)

        return output


class SearXNGSearchProvider:
    @classmethod
    def search(
        cls,
        query,
        max_results=8,
        base_url="",
    ):
        try:
            endpoint = f"{str(base_url or '').rstrip('/')}" "/search"

            response = requests.get(
                endpoint,
                params={
                    "q": query,
                    "format": "json",
                    "language": "en",
                    "safesearch": 1,
                },
                timeout=int(
                    os.getenv(
                        "WEB_SEARCH_TIMEOUT_SECONDS",
                        "10",
                    )
                ),
            )

            response.raise_for_status()

            data = response.json()

            results = []

            for item in data.get("results", [])[:max_results]:
                url = str(item.get("url") or "").strip()

                title = str(item.get("title") or "").strip()

                snippet = str(item.get("content") or "").strip()

                if not url or not title:
                    continue

                results.append(
                    {
                        "title": title,
                        "snippet": snippet,
                        "url": url,
                        "source": "searxng",
                        "published_at": str(
                            item.get("publishedDate")
                            or item.get("published_date")
                            or ""
                        ),
                    }
                )

            return {
                "results": results,
                "warnings": [],
            }

        except Exception as exc:
            return {
                "results": [],
                "warnings": [("SearXNG search unavailable: " f"{exc}")],
            }


class BraveSearchProvider:
    @classmethod
    def search(
        cls,
        query,
        max_results=8,
        api_key="",
    ):
        try:
            response = requests.get(
                ("https://api.search.brave.com/" "res/v1/web/search"),
                params={
                    "q": query,
                    "count": min(
                        max_results,
                        20,
                    ),
                    "search_lang": "en",
                    "safesearch": "moderate",
                },
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": api_key,
                },
                timeout=int(
                    os.getenv(
                        "WEB_SEARCH_TIMEOUT_SECONDS",
                        "10",
                    )
                ),
            )

            response.raise_for_status()

            data = response.json()

            results = []

            for item in (data.get("web") or {}).get(
                "results",
                [],
            )[:max_results]:
                url = str(item.get("url") or "").strip()

                title = str(item.get("title") or "").strip()

                snippet = str(item.get("description") or "").strip()

                if not url or not title:
                    continue

                results.append(
                    {
                        "title": title,
                        "snippet": snippet,
                        "url": url,
                        "source": "brave_search",
                    }
                )

            return {
                "results": results,
                "warnings": [],
            }

        except Exception as exc:
            return {
                "results": [],
                "warnings": [("Brave search unavailable: " f"{exc}")],
            }


class WikipediaSearchProvider:
    @classmethod
    def search(
        cls,
        query,
        max_results=2,
    ):
        try:
            response = requests.get(
                ("https://en.wikipedia.org/" "w/api.php"),
                params={
                    "action": "opensearch",
                    "search": query,
                    "limit": max_results,
                    "namespace": 0,
                    "format": "json",
                },
                timeout=int(
                    os.getenv(
                        "WEB_SEARCH_TIMEOUT_SECONDS",
                        "10",
                    )
                ),
            )

            response.raise_for_status()

            data = response.json()

            titles = data[1] if len(data) > 1 else []

            descriptions = data[2] if len(data) > 2 else []

            urls = data[3] if len(data) > 3 else []

            results = []

            for (
                index,
                title,
            ) in enumerate(titles[:max_results]):
                description = descriptions[index] if index < len(descriptions) else ""

                url = urls[index] if index < len(urls) else ""

                summary = cls.get_summary(title)

                if summary or description:
                    results.append(
                        {
                            "title": title,
                            "snippet": (summary or description),
                            "url": url,
                            "source": "wikipedia",
                        }
                    )

            return {
                "results": results,
                "warnings": [],
            }

        except Exception as exc:
            return {
                "results": [],
                "warnings": [("Wikipedia search unavailable: " f"{exc}")],
            }

    @staticmethod
    def get_summary(
        title,
    ):
        try:
            response = requests.get(
                (
                    "https://en.wikipedia.org/"
                    "api/rest_v1/page/summary/"
                    f"{quote(title)}"
                ),
                timeout=int(
                    os.getenv(
                        "WEB_SEARCH_TIMEOUT_SECONDS",
                        "10",
                    )
                ),
            )

            response.raise_for_status()

            return response.json().get(
                "extract",
                "",
            )

        except Exception:
            return ""


class DuckDuckGoSearchProvider:
    STOPWORDS = WebSearchProviderService.STOPWORDS

    @classmethod
    def search(
        cls,
        query,
        max_results=8,
    ):
        results = []
        warnings = []

        html_response = cls.search_html(
            query=query,
            max_results=max_results,
        )

        results.extend(html_response["results"])

        warnings.extend(html_response["warnings"])

        # Instant Answers are only fallback stable/entity
        # data, not current-news evidence.
        if len(results) < max_results and not (
            WebSearchProviderService.is_fresh_query(query)
        ):
            instant_response = cls.search_instant_answer(
                query=query,
                max_results=(max_results - len(results)),
            )

            results.extend(instant_response["results"])

            warnings.extend(instant_response["warnings"])

        return {
            "results": (WebSearchProviderService.dedupe_results(results)[:max_results]),
            "warnings": warnings,
        }

    @classmethod
    def search_html(
        cls,
        query,
        max_results=8,
    ):
        try:
            response = requests.get(
                ("https://html.duckduckgo.com/" "html/"),
                params={
                    "q": query,
                },
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; "
                        "Win64; x64) "
                        "AppleWebKit/537.36 "
                        "Chrome/120 Safari/537.36"
                    )
                },
                timeout=int(
                    os.getenv(
                        "WEB_SEARCH_TIMEOUT_SECONDS",
                        "10",
                    )
                ),
            )

            response.raise_for_status()

            parser = _DuckDuckGoHTMLParser()

            parser.feed(response.text)

            results = []

            for item in parser.results[: max_results * 2]:
                title = cls.clean_html(item.get("title") or "")

                snippet = cls.clean_html(item.get("snippet") or "")

                url = cls.clean_url(item.get("url") or "")

                if not title or not url:
                    continue

                #
                # Do not reject discovery candidates here.
                #
                # Search providers are discovery transports.
                # Semantic relevance, requested-source matching,
                # title matching, freshness, and evidence quality
                # are evaluated downstream after results from all
                # configured providers have been combined.
                #
                results.append(
                    {
                        "title": title,
                        "snippet": snippet,
                        "url": url,
                        "source": "duckduckgo_html",
                    }
                )

                if len(results) >= max_results:
                    break

            warnings = []

            if not results:
                warnings.append(
                    (
                        "DuckDuckGo HTML search "
                        "returned no parseable "
                        "discovery results."
                    )
                )

            return {
                "results": results,
                "warnings": warnings,
            }

        except Exception as exc:
            return {
                "results": [],
                "warnings": [("DuckDuckGo HTML search " f"unavailable: {exc}")],
            }

    @classmethod
    def search_instant_answer(
        cls,
        query,
        max_results=5,
    ):
        try:
            response = requests.get(
                "https://api.duckduckgo.com/",
                params={
                    "q": query,
                    "format": "json",
                    "no_html": 1,
                    "skip_disambig": 1,
                },
                timeout=int(
                    os.getenv(
                        "WEB_SEARCH_TIMEOUT_SECONDS",
                        "10",
                    )
                ),
            )

            response.raise_for_status()

            data = response.json()

            results = []

            abstract = data.get("AbstractText")

            if abstract:
                results.append(
                    {
                        "title": (data.get("Heading") or query),
                        "snippet": abstract,
                        "url": (data.get("AbstractURL") or ""),
                        "source": "duckduckgo_instant_answer",
                    }
                )

            for topic in data.get(
                "RelatedTopics",
                [],
            ):
                if len(results) >= max_results:
                    break

                if "Text" not in topic:
                    continue

                text = str(topic.get("Text") or "")

                url = str(topic.get("FirstURL") or "")

                if not cls.is_relevant(
                    query,
                    text,
                ):
                    continue

                results.append(
                    {
                        "title": text[:120],
                        "snippet": text,
                        "url": url,
                        "source": "duckduckgo_related_topic",
                    }
                )

            return {
                "results": results[:max_results],
                "warnings": [],
            }

        except Exception as exc:
            return {
                "results": [],
                "warnings": [("DuckDuckGo instant answer " f"unavailable: {exc}")],
            }

    @staticmethod
    def clean_html(
        value,
    ):
        value = re.sub(
            r"<[^>]+>",
            " ",
            str(value or ""),
        )

        return re.sub(
            r"\s+",
            " ",
            html.unescape(value),
        ).strip()

    @staticmethod
    def clean_url(
        value,
    ):
        value = html.unescape(str(value or "").strip())

        if value.startswith("//"):
            value = f"https:{value}"

        parsed = urlsplit(value)

        hostname = (parsed.hostname or "").lower()

        if hostname.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
            params = parse_qs(parsed.query)

            target = (params.get("uddg") or [""])[0]

            if target:
                return unquote(target).strip()

        return value

    @classmethod
    def is_relevant(
        cls,
        query,
        text,
    ):
        requested_domain = WebSearchProviderService.requested_domain(query)

        if requested_domain:
            return True

        query_terms = WebSearchProviderService.query_terms(query)

        if not query_terms:
            return True

        text_lower = str(text or "").lower()

        matched_terms = [term for term in query_terms if term in text_lower]

        return len(matched_terms) >= max(
            1,
            min(
                2,
                len(query_terms),
            ),
        )
