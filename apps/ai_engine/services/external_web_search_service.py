import os

from urllib.parse import (
    urlsplit,
)

from apps.ai_engine.services.web_evidence_quality_service import (
    WebEvidenceQualityService,
)

from apps.ai_engine.services.web_page_retrieval_service import (
    WebPageRetrievalService,
)

from apps.ai_engine.services.web_search_provider_service import (
    BraveSearchProvider,
    DuckDuckGoSearchProvider,
    SearXNGSearchProvider,
)


class ExternalWebSearchService:
    """
    Executes a semantic web-search plan across configured providers.

    Semantic decisions are supplied by ExternalWebPlanService.

    This layer owns only deterministic infrastructure:
      - provider fan-out
      - bounded query execution
      - deduplication
      - page retrieval
      - evidence ranking
    """

    @classmethod
    def search(
        cls,
        fallback_query,
        search_plan,
        max_results=5,
    ):
        plan = dict(search_plan or {})

        queries = cls._queries(
            fallback_query=fallback_query,
            search_plan=plan,
        )

        if not queries:
            return {
                "results": [],
                "warnings": [
                    (
                        "The semantic web plan "
                        "did not produce a usable "
                        "search query."
                    )
                ],
                "sufficient": False,
                "executed_queries": [],
            }

        max_queries = max(
            1,
            min(
                4,
                int(
                    os.getenv(
                        "WEB_SEARCH_PLAN_MAX_QUERIES",
                        "3",
                    )
                ),
            ),
        )

        discovery_limit = max(
            max_results,
            int(
                os.getenv(
                    "WEB_SEARCH_DISCOVERY_RESULTS",
                    "12",
                )
            ),
        )

        combined_results = []
        warnings = []
        executed_queries = []

        for query in queries[:max_queries]:
            executed_queries.append(query)

            response = cls._search_one(
                query=query,
                max_results=discovery_limit,
            )

            combined_results.extend(response["results"])

            warnings.extend(response["warnings"])

        deduped = cls._dedupe_results(combined_results)

        fetch_limit = max(
            max_results,
            int(
                os.getenv(
                    "WEB_PAGE_FETCH_DISCOVERY_RESULTS",
                    "10",
                )
            ),
        )

        discovery = WebEvidenceQualityService.rank_discovery(
            results=deduped,
            search_plan=plan,
            limit=fetch_limit,
        )

        enriched = WebPageRetrievalService.enrich_results(
            discovery,
            max_results=fetch_limit,
        )

        warnings.extend(enriched["warnings"])

        selected = WebEvidenceQualityService.select(
            results=enriched["results"],
            search_plan=plan,
            limit=max_results,
        )

        warnings.extend(selected["warnings"])

        return {
            "results": selected["results"],
            "warnings": cls._dedupe_strings(warnings),
            "sufficient": bool(selected["sufficient"]),
            "executed_queries": executed_queries,
        }

    @classmethod
    def _search_one(
        cls,
        query,
        max_results,
    ):
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
                "warnings": [("Web search provider " "is disabled.")],
            }

        searxng_base_url = os.getenv(
            "SEARXNG_BASE_URL",
            "",
        ).strip()

        brave_key = os.getenv(
            "BRAVE_SEARCH_API_KEY",
            "",
        ).strip()

        results = []
        warnings = []

        if (
            provider
            in {
                "auto",
                "searxng",
            }
            and searxng_base_url
        ):
            response = SearXNGSearchProvider.search(
                query=query,
                max_results=max_results,
                base_url=searxng_base_url,
            )

            results.extend(response["results"])

            warnings.extend(response["warnings"])

        elif provider == "searxng":
            warnings.append(("SEARXNG_BASE_URL " "is not configured."))

        if (
            provider
            in {
                "auto",
                "brave",
            }
            and brave_key
        ):
            response = BraveSearchProvider.search(
                query=query,
                max_results=max_results,
                api_key=brave_key,
            )

            results.extend(response["results"])

            warnings.extend(response["warnings"])

        elif provider == "brave":
            warnings.append(("BRAVE_SEARCH_API_KEY " "is not configured."))

        #
        # DuckDuckGo HTML remains an infrastructure fallback.
        # We intentionally call search_html() directly so the
        # old keyword-based freshness heuristic is not involved
        # in the active External AI path.
        #
        if provider in {
            "auto",
            "duckduckgo",
        }:
            response = DuckDuckGoSearchProvider.search_html(
                query=query,
                max_results=max_results,
            )

            results.extend(response["results"])

            warnings.extend(response["warnings"])

        return {
            "results": results,
            "warnings": warnings,
        }

    @staticmethod
    def _queries(
        fallback_query,
        search_plan,
    ):
        candidates = list(search_plan.get("search_queries") or [])

        fallback = str(fallback_query or "").strip()

        #
        # The fallback query exists for degraded plans.
        # Do not add an extra network search when the
        # semantic planner already supplied queries.
        #
        if not candidates and fallback:
            candidates.append(fallback)

        output = []
        seen = set()

        for candidate in candidates:
            query = " ".join(str(candidate or "").split())[:700]

            key = query.casefold()

            if not query or key in seen:
                continue

            seen.add(key)

            output.append(query)

            if len(output) >= 4:
                break

        return output

    @staticmethod
    def _dedupe_results(
        results,
    ):
        output = []
        seen = set()

        for raw in results or []:
            item = dict(raw or {})

            url = str(item.get("url") or "").strip()

            parsed = urlsplit(url)

            key = (
                (
                    f"{parsed.scheme.lower()}"
                    f"://"
                    f"{parsed.netloc.lower()}"
                    f"{parsed.path.rstrip('/')}"
                )
                if (parsed.scheme and parsed.netloc)
                else (url or str(item.get("title") or "").strip().casefold())
            )

            if not key or key in seen:
                continue

            seen.add(key)

            output.append(item)

        return output

    @staticmethod
    def _dedupe_strings(
        values,
    ):
        output = []
        seen = set()

        for value in values or []:
            text = str(value or "").strip()

            if not text or text in seen:
                continue

            seen.add(text)

            output.append(text)

        return output
