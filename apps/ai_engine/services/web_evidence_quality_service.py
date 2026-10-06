import re

from datetime import (
    datetime,
    timezone,
)

from difflib import (
    SequenceMatcher,
)

from urllib.parse import (
    urlsplit,
)


class WebEvidenceQualityService:
    """
    Generic evidence scorer.

    Ranking is derived from:
      - semantic source preference
      - semantic exact-title preference
      - question/result relevance
      - fetched page availability
      - publication recency when available

    There are no publisher/person/query-specific rules.
    """

    @classmethod
    def rank_discovery(
        cls,
        results,
        search_plan,
        limit=12,
    ):
        plan = dict(search_plan or {})

        requested_source = str(plan.get("requested_source") or "").strip()

        exact_title = str(plan.get("exact_title") or "").strip()

        resolved_question = str(plan.get("resolved_question") or "").strip()

        scored = []

        for index, raw in enumerate(results or []):
            item = dict(raw or {})

            source_score = cls._source_affinity(
                requested_source,
                item,
            )

            title_score = cls._title_affinity(
                exact_title,
                item.get("title"),
            )

            relevance_score = cls._text_relevance(
                resolved_question,
                " ".join(
                    [
                        str(item.get("title") or ""),
                        str(item.get("snippet") or ""),
                    ]
                ),
            )

            score = source_score * 50.0 + title_score * 40.0 + relevance_score * 20.0

            scored.append(
                (
                    score,
                    -index,
                    item,
                )
            )

        scored.sort(
            key=lambda row: (
                row[0],
                row[1],
            ),
            reverse=True,
        )

        return [row[2] for row in scored[:limit]]

    @classmethod
    def select(
        cls,
        results,
        search_plan,
        limit=5,
    ):
        plan = dict(search_plan or {})

        requested_source = str(plan.get("requested_source") or "").strip()

        exact_title = str(plan.get("exact_title") or "").strip()

        resolved_question = str(plan.get("resolved_question") or "").strip()

        query_type = str(plan.get("query_type") or "web_research").strip().lower()

        freshness = str(plan.get("freshness") or "none").strip().lower()

        candidates = []

        for index, raw in enumerate(results or []):
            item = dict(raw or {})

            content = str(item.get("content") or "").strip()

            #
            # Search snippets are discovery material,
            # not final factual evidence.
            #
            if not content:
                continue

            source_score = cls._source_affinity(
                requested_source,
                item,
            )

            title_score = cls._title_affinity(
                exact_title,
                item.get("title"),
            )

            relevance_score = cls._text_relevance(
                resolved_question,
                " ".join(
                    [
                        str(item.get("title") or ""),
                        str(item.get("snippet") or ""),
                        content[:2500],
                    ]
                ),
            )

            freshness_score = cls._freshness_score(
                freshness,
                item.get("published_at"),
            )

            score = (
                20.0
                + source_score * 35.0
                + title_score * 35.0
                + relevance_score * 20.0
                + freshness_score
            )

            item["evidence_score"] = round(
                score,
                4,
            )

            item["source_match_score"] = round(
                source_score,
                4,
            )

            item["title_match_score"] = round(
                title_score,
                4,
            )

            candidates.append(
                (
                    score,
                    -index,
                    item,
                )
            )

        warnings = []

        #
        # Explicit source requests fail closed if
        # fetched evidence cannot be tied back to
        # that source.
        #
        if requested_source:
            source_matches = [
                row
                for row in candidates
                if row[2].get(
                    "source_match_score",
                    0.0,
                )
                >= 0.75
            ]

            if source_matches:
                candidates = source_matches

            elif query_type in {
                "article_lookup",
                "source_lookup",
            }:
                warnings.append(
                    (
                        "No fetched evidence "
                        "matched the explicitly "
                        "requested source."
                    )
                )

                return {
                    "results": [],
                    "warnings": warnings,
                    "sufficient": False,
                }

        #
        # Exact page/article lookup also fails closed
        # when the fetched result title is not close
        # enough to the requested title.
        #
        if exact_title:
            title_matches = [
                row
                for row in candidates
                if row[2].get(
                    "title_match_score",
                    0.0,
                )
                >= 0.50
            ]

            if title_matches:
                candidates = title_matches

            elif query_type == "article_lookup":
                warnings.append(
                    (
                        "No fetched evidence "
                        "matched the requested "
                        "page/article title."
                    )
                )

                return {
                    "results": [],
                    "warnings": warnings,
                    "sufficient": False,
                }

        candidates.sort(
            key=lambda row: (
                row[0],
                row[1],
            ),
            reverse=True,
        )

        selected = [row[2] for row in candidates[:limit]]

        if not selected:
            warnings.append(
                (
                    "Search returned no fetched "
                    "page content suitable for "
                    "grounding."
                )
            )

        return {
            "results": selected,
            "warnings": warnings,
            "sufficient": bool(selected),
        }

    @classmethod
    def _source_affinity(
        cls,
        requested_source,
        item,
    ):
        if not requested_source:
            return 0.0

        source_tokens = cls._tokens(requested_source)

        if not source_tokens:
            return 0.0

        url = str(item.get("url") or "")

        hostname = urlsplit(url).hostname or ""

        haystack = " ".join(
            [
                hostname,
                str(item.get("title") or ""),
                str(item.get("source") or ""),
            ]
        )

        haystack_tokens = cls._tokens(haystack)

        overlap = len(source_tokens & haystack_tokens) / max(
            1,
            len(source_tokens),
        )

        compact_source = cls._compact(requested_source)

        compact_haystack = cls._compact(haystack)

        if compact_source and compact_source in compact_haystack:
            return 1.0

        return min(
            1.0,
            overlap,
        )

    @classmethod
    def _title_affinity(
        cls,
        exact_title,
        result_title,
    ):
        if not exact_title:
            return 0.0

        left = cls._normalize_text(exact_title)

        right = cls._normalize_text(result_title)

        if not left or not right:
            return 0.0

        sequence = SequenceMatcher(
            None,
            left,
            right,
        ).ratio()

        left_tokens = cls._tokens(left)

        right_tokens = cls._tokens(right)

        union = left_tokens | right_tokens

        token_score = len(left_tokens & right_tokens) / max(
            1,
            len(union),
        )

        return max(
            sequence,
            token_score,
        )

    @classmethod
    def _text_relevance(
        cls,
        query,
        text,
    ):
        query_tokens = cls._tokens(query)

        if not query_tokens:
            return 0.0

        text_tokens = cls._tokens(text)

        return min(
            1.0,
            len(query_tokens & text_tokens)
            / max(
                1,
                len(query_tokens),
            ),
        )

    @staticmethod
    def _freshness_score(
        freshness,
        published_at,
    ):
        if freshness not in {
            "current",
            "recent",
        }:
            return 0.0

        value = str(published_at or "").strip()

        if not value:
            return 0.0

        try:
            parsed = datetime.fromisoformat(
                value.replace(
                    "Z",
                    "+00:00",
                )
            )

            if parsed.tzinfo is None:
                parsed = parsed.replace(
                    tzinfo=timezone.utc,
                )

            age_days = max(
                0.0,
                (
                    datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
                ).total_seconds()
                / 86400.0,
            )

        except Exception:
            return 0.0

        if age_days <= 2:
            return 12.0

        if age_days <= 14:
            return 8.0

        if age_days <= 45:
            return 4.0

        return 0.0

    @staticmethod
    def _normalize_text(
        value,
    ):
        return re.sub(
            r"\s+",
            " ",
            str(value or "").casefold(),
        ).strip()

    @classmethod
    def _tokens(
        cls,
        value,
    ):
        return {
            token
            for token in re.findall(
                r"[a-z0-9]+",
                cls._normalize_text(value),
            )
            if (len(token) >= 2)
        }

    @staticmethod
    def _compact(
        value,
    ):
        return "".join(
            re.findall(
                r"[a-z0-9]+",
                str(value or "").casefold(),
            )
        )
