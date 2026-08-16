import os
import re
from urllib.parse import quote

import requests


class WebSearchProviderService:
    @classmethod
    def search(cls, query, max_results=5):
        return cls.search_with_metadata(query=query, max_results=max_results)["results"]

    @classmethod
    def search_with_metadata(cls, query, max_results=5):
        provider = os.getenv("WEB_SEARCH_PROVIDER", "duckduckgo").lower()

        if provider == "disabled":
            return {
                "results": [],
                "warnings": ["Web search provider is disabled."],
            }

        all_results = []
        warnings = []

        wikipedia_response = WikipediaSearchProvider.search(query=query, max_results=2)
        all_results.extend(wikipedia_response["results"])
        warnings.extend(wikipedia_response["warnings"])

        if provider in ["duckduckgo", "auto"]:
            duck_response = DuckDuckGoSearchProvider.search(
                query=query,
                max_results=max_results,
            )
            all_results.extend(duck_response["results"])
            warnings.extend(duck_response["warnings"])

        results = cls.dedupe_results(all_results)
        results = cls.filter_low_quality_results(query, results)[:max_results]

        return {
            "results": results,
            "warnings": warnings,
        }

    @classmethod
    def filter_low_quality_results(cls, query, results):
        query_terms = {
            term for term in re.findall(r"[a-zA-Z0-9]+", query.lower()) if len(term) > 2
        }

        if not query_terms:
            return results

        filtered = []

        for result in results:
            combined = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
            matched = [term for term in query_terms if term in combined]

            if len(matched) >= 1:
                filtered.append(result)

        return filtered

    @staticmethod
    def dedupe_results(results):
        seen = set()
        output = []

        for result in results:
            url = result.get("url") or ""
            title = result.get("title") or ""
            key = url or title

            if not key:
                continue

            if key in seen:
                continue

            seen.add(key)
            output.append(result)

        return output


class WikipediaSearchProvider:
    @classmethod
    def search(cls, query, max_results=2):
        try:
            response = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "opensearch",
                    "search": query,
                    "limit": max_results,
                    "namespace": 0,
                    "format": "json",
                },
                timeout=int(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "10")),
            )
            response.raise_for_status()

            data = response.json()

            titles = data[1] if len(data) > 1 else []
            descriptions = data[2] if len(data) > 2 else []
            urls = data[3] if len(data) > 3 else []

            results = []

            for index, title in enumerate(titles[:max_results]):
                description = descriptions[index] if index < len(descriptions) else ""
                url = urls[index] if index < len(urls) else ""

                summary = cls.get_summary(title)

                if summary or description:
                    results.append(
                        {
                            "title": title,
                            "snippet": summary or description,
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
                "warnings": [f"Wikipedia search unavailable: {exc}"],
            }

    @staticmethod
    def get_summary(title):
        try:
            response = requests.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title)}",
                timeout=int(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "10")),
            )
            response.raise_for_status()

            data = response.json()
            return data.get("extract", "")

        except Exception:
            return ""


class DuckDuckGoSearchProvider:
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
        "latest",
        "tell",
        "me",
        "about",
        "please",
        "can",
        "you",
    }

    @classmethod
    def search(cls, query, max_results=5):
        results = []
        warnings = []

        instant_response = cls.search_instant_answer(
            query=query,
            max_results=max_results,
        )
        results.extend(instant_response["results"])
        warnings.extend(instant_response["warnings"])

        html_response = cls.search_html(
            query=query,
            max_results=max_results,
        )
        results.extend(html_response["results"])
        warnings.extend(html_response["warnings"])

        return {
            "results": WebSearchProviderService.dedupe_results(results)[:max_results],
            "warnings": warnings,
        }

    @classmethod
    def search_instant_answer(cls, query, max_results=5):
        try:
            response = requests.get(
                "https://api.duckduckgo.com/",
                params={
                    "q": query,
                    "format": "json",
                    "no_html": 1,
                    "skip_disambig": 1,
                },
                timeout=int(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "10")),
            )
            response.raise_for_status()
            data = response.json()

            results = []

            abstract = data.get("AbstractText")
            if abstract:
                results.append(
                    {
                        "title": data.get("Heading") or query,
                        "snippet": abstract,
                        "url": data.get("AbstractURL") or "",
                        "source": "duckduckgo_instant_answer",
                    }
                )

            for topic in data.get("RelatedTopics", []):
                if len(results) >= max_results:
                    break

                if "Text" not in topic:
                    continue

                text = topic.get("Text", "")
                url = topic.get("FirstURL", "")

                if not cls.is_relevant(query, text):
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
                "warnings": [f"DuckDuckGo instant answer unavailable: {exc}"],
            }

    @classmethod
    def search_html(cls, query, max_results=5):
        try:
            response = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                    )
                },
                timeout=int(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "10")),
            )
            response.raise_for_status()

            html = response.text

            result_blocks = re.findall(
                r'<a rel="nofollow" class="result__a" href="(?P<url>.*?)".*?>(?P<title>.*?)</a>.*?'
                r'<a class="result__snippet".*?>(?P<snippet>.*?)</a>',
                html,
                flags=re.S,
            )

            results = []

            for raw_url, raw_title, raw_snippet in result_blocks[:max_results]:
                title = cls.clean_html(raw_title)
                snippet = cls.clean_html(raw_snippet)
                url = cls.clean_url(raw_url)

                if not title or not snippet:
                    continue

                if not cls.is_relevant(query, f"{title} {snippet}"):
                    continue

                results.append(
                    {
                        "title": title,
                        "snippet": snippet,
                        "url": url,
                        "source": "duckduckgo_html",
                    }
                )

            return {
                "results": results,
                "warnings": [],
            }

        except Exception as exc:
            return {
                "results": [],
                "warnings": [f"DuckDuckGo HTML search unavailable: {exc}"],
            }

    @staticmethod
    def clean_html(value):
        value = re.sub(r"<[^>]+>", " ", value)
        value = value.replace("&amp;", "&")
        value = value.replace("&quot;", '"')
        value = value.replace("&#x27;", "'")
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    @staticmethod
    def clean_url(value):
        value = value.replace("&amp;", "&")
        return value.strip()

    @classmethod
    def is_relevant(cls, query, text):
        query_terms = {
            term
            for term in re.findall(r"[a-zA-Z]+", query.lower())
            if term not in cls.STOPWORDS and len(term) > 2
        }

        if not query_terms:
            return True

        text_lower = text.lower()

        matched_terms = [term for term in query_terms if term in text_lower]

        return len(matched_terms) >= max(1, min(2, len(query_terms)))
