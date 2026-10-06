from urllib.parse import urlsplit

from apps.ai_engine.services.web_evidence_quality_service import (
    WebEvidenceQualityService,
)


class WebDiscoveryEvidenceService:
    """
    Converts high-quality web-search discovery results into bounded
    secondary evidence when publisher-page retrieval is unavailable.

    Preferred evidence remains fetched publisher-page content.

    This service contains no:
      - person-specific knowledge
      - publisher-specific mappings
      - political-role mappings
      - article-specific rules
      - current factual answers
    """

    @classmethod
    def select(
        cls,
        discovery_results,
        search_plan,
        limit=5,
    ):
        plan = dict(search_plan or {})

        query_type = str(plan.get("query_type") or "web_research").strip().lower()

        requested_source = str(plan.get("requested_source") or "").strip()

        exact_title = str(plan.get("exact_title") or "").strip()

        #
        # Explaining a specific article/page requires stronger
        # page-level evidence. A search snippet alone is not enough
        # to claim we have read or summarized the requested article.
        #
        if query_type == "article_lookup" or exact_title:
            return {
                "results": [],
                "warnings": [
                    (
                        "The requested page/article "
                        "could not be retrieved with "
                        "enough page-level evidence."
                    )
                ],
                "sufficient": False,
                "evidence_mode": "none",
            }

        ranked = WebEvidenceQualityService.rank_discovery(
            results=discovery_results,
            search_plan=plan,
            limit=max(
                limit * 4,
                12,
            ),
        )

        selected = []
        seen_urls = set()
        seen_hosts = set()

        for raw in ranked:
            item = dict(raw or {})

            title = str(item.get("title") or "").strip()

            snippet = str(item.get("snippet") or "").strip()

            url = str(item.get("url") or "").strip()

            if not title or not snippet or not url:
                continue

            parsed = urlsplit(url)

            if parsed.scheme.lower() not in {
                "http",
                "https",
            }:
                continue

            hostname = (parsed.hostname or "").casefold()

            if not hostname:
                continue

            normalized_url = (
                f"{parsed.scheme.lower()}"
                f"://"
                f"{parsed.netloc.lower()}"
                f"{parsed.path.rstrip('/')}"
            )

            if normalized_url in seen_urls:
                continue

            source_match = 0.0

            if requested_source:
                source_match = WebEvidenceQualityService._source_affinity(
                    requested_source,
                    item,
                )

                #
                # Explicit source requests should still be related
                # to the source the user actually requested.
                #
                if source_match < 0.50:
                    continue

            seen_urls.add(normalized_url)

            seen_hosts.add(hostname)

            item["content"] = snippet

            item["content_source"] = "search_result_snippet"

            item["evidence_kind"] = "search_result_snippet"

            item["evidence_strength"] = "secondary"

            item["source_match_score"] = round(
                source_match,
                4,
            )

            selected.append(item)

            if len(selected) >= limit:
                break

        if not selected:
            return {
                "results": [],
                "warnings": [("Web search returned no " "usable discovery evidence.")],
                "sufficient": False,
                "evidence_mode": "none",
            }

        #
        # Current factual verification needs corroboration when
        # we have only snippets and no successfully fetched page.
        #
        if query_type == "current_fact":
            sufficient = len(seen_hosts) >= 2

        else:
            #
            # News/source discovery/research can still be useful
            # with one or more genuine search results.
            #
            sufficient = len(selected) >= 1

        if not sufficient:
            return {
                "results": [],
                "warnings": [
                    (
                        "Web search results were "
                        "found, but there was not "
                        "enough corroboration for "
                        "the current factual claim."
                    )
                ],
                "sufficient": False,
                "evidence_mode": "none",
            }

        return {
            "results": selected,
            "warnings": [],
            "sufficient": True,
            "evidence_mode": "search_discovery",
        }
