class ExternalWebPlanService:
    """
    Converts the existing semantic-router decision into
    a bounded web-retrieval plan.

    This service performs no model call and contains no
    person-, publisher-, article-, country-, or query-
    specific knowledge.
    """

    WEB_ACTIONS = {
        "web",
        "document_web",
    }

    VALID_FRESHNESS = {
        "none",
        "current",
        "recent",
        "historical",
    }

    VALID_QUERY_TYPES = {
        "stable_knowledge",
        "current_fact",
        "news",
        "article_lookup",
        "source_lookup",
        "web_research",
    }

    @classmethod
    def from_routing_plan(
        cls,
        message,
        routing_plan,
        use_web=True,
    ):
        routing = dict(routing_plan or {})

        action = str(routing.get("action") or "").strip().lower()

        needs_web = bool(routing.get("web_required")) or (action in cls.WEB_ACTIONS)

        if not needs_web:
            return {
                "needs_web": False,
                "freshness": "none",
                "query_type": "stable_knowledge",
                "resolved_question": str(message or "").strip()[:900],
                "requested_source": "",
                "exact_title": "",
                "claim_to_verify": "",
                "search_queries": [],
                "web_enabled": bool(use_web),
            }

        freshness = str(routing.get("web_freshness") or "current").strip().lower()

        if freshness not in cls.VALID_FRESHNESS:
            freshness = "current"

        query_type = (
            str(routing.get("web_query_type") or "web_research").strip().lower()
        )

        if query_type not in cls.VALID_QUERY_TYPES:
            query_type = "web_research"

        resolved_question = cls._bounded(
            routing.get("web_resolved_question") or message,
            900,
        )

        requested_source = cls._bounded(
            routing.get("web_requested_source"),
            180,
        )

        exact_title = cls._bounded(
            routing.get("web_exact_title"),
            500,
        )

        claim_to_verify = cls._bounded(
            routing.get("web_claim_to_verify"),
            900,
        )

        if exact_title:
            query_type = "article_lookup"

        elif (
            freshness
            in {
                "current",
                "recent",
            }
            and query_type == "stable_knowledge"
        ):
            query_type = "web_research"

        semantic_queries = []

        if exact_title:
            semantic_queries.append(
                cls._compose_query(
                    f'"{exact_title}"',
                    requested_source,
                )
            )

        #
        # Always preserve the literal user request.
        # Semantic paraphrases can supplement it but
        # cannot replace its entities/title/context.
        #
        semantic_queries.append(
            cls._compose_query(
                message,
                requested_source,
            )
        )

        if claim_to_verify:
            semantic_queries.append(
                cls._compose_query(
                    claim_to_verify,
                    requested_source,
                )
            )

        semantic_queries.extend(routing.get("web_search_queries") or [])

        if routing.get("web_query"):
            semantic_queries.append(routing["web_query"])

        semantic_queries.append(resolved_question)

        return {
            "needs_web": True,
            "freshness": freshness,
            "query_type": query_type,
            "resolved_question": resolved_question,
            "requested_source": requested_source,
            "exact_title": exact_title,
            "claim_to_verify": claim_to_verify,
            "search_queries": cls._normalize_queries(semantic_queries)[:4],
            "web_enabled": bool(use_web),
        }

    @classmethod
    def fallback_for_uncertain_route(
        cls,
        message,
        use_web=True,
    ):
        message = str(message or "").strip()

        if not use_web or not message:
            return None

        return {
            "needs_web": True,
            "freshness": "current",
            "query_type": "web_research",
            "resolved_question": message[:900],
            "requested_source": "",
            "exact_title": "",
            "claim_to_verify": "",
            "search_queries": [message[:700]],
            "web_enabled": True,
            "fallback_reason": "semantic_router_uncertain",
        }

    @staticmethod
    def _bounded(
        value,
        limit,
    ):
        return str(value or "").strip()[:limit]

    @staticmethod
    def _compose_query(
        first,
        second,
    ):
        return " ".join(
            part
            for part in (
                str(first or "").strip(),
                str(second or "").strip(),
            )
            if part
        ).strip()

    @classmethod
    def _normalize_queries(
        cls,
        values,
    ):
        if isinstance(
            values,
            str,
        ):
            values = [values]

        if not isinstance(
            values,
            (
                list,
                tuple,
            ),
        ):
            return []

        output = []
        seen = set()

        for raw in values:
            query = " ".join(str(raw or "").split())[:700]

            key = query.casefold()

            if not query or key in seen:
                continue

            seen.add(key)

            output.append(query)

            if len(output) >= 4:
                break

        return output
