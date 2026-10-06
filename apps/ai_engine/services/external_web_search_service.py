import logging
import os

from urllib.parse import urlsplit

from apps.ai_engine.services.web_discovery_evidence_service import (
    WebDiscoveryEvidenceService,
)

from apps.ai_engine.services.web_evidence_quality_service import (
    WebEvidenceQualityService,
)

from apps.ai_engine.services.web_page_retrieval_service import (
    WebPageRetrievalService,
)

from apps.ai_engine.services.web_search_fallback_provider_service import (
    BingRSSSearchProvider,
    DuckDuckGoLiteSearchProvider,
)

from apps.ai_engine.services.web_search_provider_service import (
    BraveSearchProvider,
    DuckDuckGoSearchProvider,
    SearXNGSearchProvider,
)


logger = logging.getLogger(__name__)


class ExternalWebSearchService:
    """
    Public web evidence pipeline.

    semantic plan
      -> provider discovery cascade
      -> dedupe
      -> relevance ranking
      -> publisher fetch
      -> primary evidence
      -> bounded discovery evidence fallback
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
                "warnings": [("No usable web search " "query was produced.")],
                "diagnostics": [],
                "sufficient": False,
                "evidence_mode": "none",
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

        discovery = []
        diagnostics = []
        executed_queries = []

        for query in queries[:max_queries]:
            executed_queries.append(query)

            response = cls._search_one(
                query=query,
                max_results=discovery_limit,
            )

            discovery.extend(response.get("results") or [])

            diagnostics.extend(response.get("warnings") or [])

        discovery = cls._dedupe_results(discovery)

        if not discovery:
            diagnostics = cls._dedupe_strings(diagnostics)

            logger.warning(
                (
                    "external_web_search_complete "
                    "queries=%s discovery=0 "
                    "fetched=0 selected=0 "
                    "sufficient=False "
                    "evidence_mode=none "
                    "diagnostics=%s"
                ),
                len(executed_queries),
                diagnostics,
            )

            return {
                "results": [],
                "warnings": [
                    ("No public web search " "provider returned usable " "results.")
                ],
                "diagnostics": diagnostics,
                "sufficient": False,
                "evidence_mode": "none",
                "executed_queries": executed_queries,
            }

        fetch_limit = max(
            max_results,
            int(
                os.getenv(
                    "WEB_PAGE_FETCH_DISCOVERY_RESULTS",
                    "10",
                )
            ),
        )

        ranked = WebEvidenceQualityService.rank_discovery(
            results=discovery,
            search_plan=plan,
            limit=fetch_limit,
        )

        enriched = WebPageRetrievalService.enrich_results(
            ranked,
            max_results=fetch_limit,
        )

        diagnostics.extend(enriched.get("warnings") or [])

        primary = WebEvidenceQualityService.select(
            results=enriched.get("results") or [],
            search_plan=plan,
            limit=max_results,
        )

        diagnostics.extend(primary.get("warnings") or [])

        if primary.get("sufficient"):
            selected = primary

            evidence_mode = "publisher_page"

        else:
            selected = WebDiscoveryEvidenceService.select(
                discovery_results=ranked,
                search_plan=plan,
                limit=max_results,
            )

            diagnostics.extend(selected.get("warnings") or [])

            evidence_mode = selected.get("evidence_mode") or "none"

        selected_results = list(selected.get("results") or [])

        sufficient = bool(selected.get("sufficient"))

        fetched_count = sum(
            1
            for item in (enriched.get("results") or [])
            if str(item.get("content") or "").strip()
        )

        diagnostics = cls._dedupe_strings(diagnostics)

        logger.info(
            (
                "external_web_search_complete "
                "queries=%s discovery=%s "
                "fetched=%s selected=%s "
                "sufficient=%s "
                "evidence_mode=%s"
            ),
            len(executed_queries),
            len(discovery),
            fetched_count,
            len(selected_results),
            sufficient,
            evidence_mode,
        )

        return {
            "results": selected_results,
            "warnings": ([] if sufficient else diagnostics),
            "diagnostics": diagnostics,
            "sufficient": sufficient,
            "evidence_mode": evidence_mode,
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

        searxng_url = (
            os.getenv(
                "SEARXNG_BASE_URL",
                "",
            )
            .strip()
            .rstrip("/")
        )

        brave_key = os.getenv(
            "BRAVE_SEARCH_API_KEY",
            "",
        ).strip()

        results = []
        warnings = []

        def merge(
            name,
            response,
        ):
            rows = list(response.get("results") or [])

            provider_warnings = list(response.get("warnings") or [])

            results.extend(rows)

            warnings.extend(provider_warnings)

            logger.info(
                ("external_web_provider " "provider=%s results=%s"),
                name,
                len(rows),
            )

        #
        # Preferred private/self-hosted search gateway.
        #
        if (
            provider
            in {
                "auto",
                "searxng",
            }
            and searxng_url
        ):
            merge(
                "searxng",
                SearXNGSearchProvider.search(
                    query=query,
                    max_results=max_results,
                    base_url=searxng_url,
                ),
            )

        if provider == "searxng":
            if not searxng_url:
                warnings.append(("SEARXNG_BASE_URL " "is not configured."))

            return {
                "results": cls._dedupe_results(results)[:max_results],
                "warnings": warnings,
            }

        #
        # Optional managed search API.
        #
        if (
            provider
            in {
                "auto",
                "brave",
            }
            and brave_key
            and len(results) < max_results
        ):
            merge(
                "brave",
                BraveSearchProvider.search(
                    query=query,
                    max_results=max_results,
                    api_key=brave_key,
                ),
            )

        if provider == "brave":
            if not brave_key:
                warnings.append(("BRAVE_SEARCH_API_KEY " "is not configured."))

            return {
                "results": cls._dedupe_results(results)[:max_results],
                "warnings": warnings,
            }

        #
        # Best-effort no-key transports.
        #
        if (
            provider
            in {
                "auto",
                "duckduckgo",
            }
            and len(results) < max_results
        ):
            merge(
                "duckduckgo_html",
                DuckDuckGoSearchProvider.search_html(
                    query=query,
                    max_results=max_results,
                ),
            )

        if (
            provider
            in {
                "auto",
                "duckduckgo",
            }
            and len(results) < max_results
        ):
            merge(
                "duckduckgo_lite",
                DuckDuckGoLiteSearchProvider.search(
                    query=query,
                    max_results=max_results,
                ),
            )

        if provider == "duckduckgo":
            return {
                "results": cls._dedupe_results(results)[:max_results],
                "warnings": warnings,
            }

        #
        # Independent no-key fallback so DDG blocking does not
        # collapse all public web capability.
        #
        if (
            provider
            in {
                "auto",
                "bing",
            }
            and len(results) < max_results
        ):
            merge(
                "bing_rss",
                BingRSSSearchProvider.search(
                    query=query,
                    max_results=max_results,
                ),
            )

        return {
            "results": cls._dedupe_results(results)[:max_results],
            "warnings": warnings,
        }

    @staticmethod
    def _queries(
        fallback_query,
        search_plan,
    ):
        candidates = list(search_plan.get("search_queries") or [])

        #
        # A semantic planner's queries are authoritative.
        # Only fall back to the resolved/raw question when the
        # planner did not produce any discovery query.
        #
        if not candidates:
            resolved = str(search_plan.get("resolved_question") or "").strip()

            if resolved:
                candidates.append(resolved)

        if not candidates:
            fallback = str(fallback_query or "").strip()

            if fallback:
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

            if parsed.scheme and parsed.netloc:
                key = (
                    f"{parsed.scheme.lower()}"
                    f"://"
                    f"{parsed.netloc.lower()}"
                    f"{parsed.path.rstrip('/')}"
                )

            else:
                key = url.casefold() or str(item.get("title") or "").strip().casefold()

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

        for raw in values or []:
            value = str(raw or "").strip()

            if not value or value in seen:
                continue

            seen.add(value)

            output.append(value)

        return output
