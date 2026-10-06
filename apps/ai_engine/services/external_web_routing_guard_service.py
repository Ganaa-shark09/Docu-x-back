import re


class ExternalWebRoutingGuardService:
    """
    Deterministic safety boundary for information whose accuracy
    depends on fresh public evidence.

    This service:
      - never answers a factual question
      - contains no person-specific rules
      - contains no publisher-specific rules
      - contains no organization/domain mappings
      - contains no current factual values

    Its only job is deciding when model-memory-only answering is
    unsafe because fresh web evidence is required.
    """

    WEB_ACTIONS = {
        "web",
        "document_web",
    }

    FRESHNESS_PATTERN = re.compile(
        (
            r"\b("
            r"today(?:'s|s)?|"
            r"tonight|"
            r"now|"
            r"current(?:ly)?|"
            r"latest|"
            r"breaking|"
            r"live|"
            r"recent(?:ly)?|"
            r"news|"
            r"headlines?|"
            r"this\s+week|"
            r"this\s+month|"
            r"this\s+year|"
            r"as\s+of"
            r")\b"
        ),
        re.IGNORECASE,
    )

    VOLATILE_PATTERN = re.compile(
        (
            r"\b("
            r"weather|"
            r"forecast|"
            r"score|"
            r"scores|"
            r"standings|"
            r"price|"
            r"prices|"
            r"market|"
            r"stock|"
            r"stocks|"
            r"election|"
            r"results?|"
            r"winner|"
            r"outage|"
            r"availability|"
            r"appointed|"
            r"resigned|"
            r"released?|"
            r"launch(?:ed)?"
            r")\b"
        ),
        re.IGNORECASE,
    )

    EXPLICIT_WEB_PATTERN = re.compile(
        (
            r"\b("
            r"search\s+(?:the\s+)?web|"
            r"web\s+search|"
            r"browse\s+(?:the\s+)?web|"
            r"look\s+up\s+online|"
            r"search\s+online|"
            r"website|"
            r"webpage|"
            r"web\s+page"
            r")\b"
        ),
        re.IGNORECASE,
    )

    NEWS_PATTERN = re.compile(
        r"\b(news|headlines?|breaking)\b",
        re.IGNORECASE,
    )

    @classmethod
    def requires_web(
        cls,
        message,
        previous_user_messages=None,
    ):
        text = str(message or "").strip()

        if not text:
            return False

        if (
            cls.FRESHNESS_PATTERN.search(text)
            or cls.VOLATILE_PATTERN.search(text)
            or cls.EXPLICIT_WEB_PATTERN.search(text)
        ):
            return True

        #
        # Short conversational follow-ups inherit the need for
        # fresh evidence when the immediately preceding user
        # request clearly required fresh public information.
        #
        words = re.findall(
            r"\w+",
            text,
        )

        if len(words) <= 12 and previous_user_messages:
            for previous in list(previous_user_messages)[:2]:
                previous_text = str(previous or "").strip()

                if previous_text and (
                    cls.FRESHNESS_PATTERN.search(previous_text)
                    or cls.VOLATILE_PATTERN.search(previous_text)
                    or cls.EXPLICIT_WEB_PATTERN.search(previous_text)
                ):
                    return True

        return False

    @classmethod
    def query_type(
        cls,
        message,
    ):
        text = str(message or "")

        if cls.NEWS_PATTERN.search(text):
            return "news"

        if cls.FRESHNESS_PATTERN.search(text) or cls.VOLATILE_PATTERN.search(text):
            return "current_fact"

        return "web_research"

    @classmethod
    def enforce_plan(
        cls,
        plan,
        message,
        use_web=True,
        previous_user_messages=None,
    ):
        result = dict(plan or {})

        action = str(result.get("action") or "").strip().lower()

        already_requires_web = (
            bool(result.get("web_required")) or action in cls.WEB_ACTIONS
        )

        safety_requires_web = cls.requires_web(
            message=message,
            previous_user_messages=(previous_user_messages),
        )

        if not (already_requires_web or safety_requires_web):
            return result

        if not use_web:
            return result

        document_required = result.get(
            "document_dependency"
        ) == "required" or action in {
            "document",
            "document_web",
        }

        result["action"] = "document_web" if document_required else "web"

        result["web_required"] = True

        if not result.get("web_freshness") or result.get("web_freshness") == "none":
            result["web_freshness"] = "current"

        if (
            not result.get("web_query_type")
            or result.get("web_query_type") == "stable_knowledge"
        ):
            result["web_query_type"] = cls.query_type(message)

        if not result.get("web_resolved_question"):
            result["web_resolved_question"] = str(message or "").strip()[:900]

        existing_queries = list(result.get("web_search_queries") or [])

        literal_query = str(message or "").strip()[:700]

        queries = []
        seen = set()

        for query in (
            [literal_query] + existing_queries + [result.get("web_query") or ""]
        ):
            query = " ".join(str(query or "").split())

            key = query.casefold()

            if not query or key in seen:
                continue

            seen.add(key)
            queries.append(query)

            if len(queries) >= 4:
                break

        result["web_search_queries"] = queries

        if queries:
            result["web_query"] = queries[0]

        result["fast_answer"] = ""

        if result.get("action") != "clarify":
            result["clarification"] = ""

        metadata = dict(result.get("metadata") or {})

        if safety_requires_web:
            metadata["web_routing_guard"] = {
                "performed": True,
                "reason": ("fresh_public_" "evidence_required"),
            }

        result["metadata"] = metadata

        return result

    @classmethod
    def build_web_plan(
        cls,
        message,
    ):
        message = str(message or "").strip()

        return {
            "action": "web",
            "answer_tier": "full",
            "clarification": "",
            "fast_answer": "",
            "document_dependency": "none",
            "document_query": "",
            "document_task": "none",
            "web_query": message[:700],
            "web_required": True,
            "web_freshness": "current",
            "web_query_type": cls.query_type(message),
            "web_resolved_question": message[:900],
            "web_requested_source": "",
            "web_exact_title": "",
            "web_claim_to_verify": "",
            "web_search_queries": [message[:700]],
            "task_domain": "general",
            "answer_profile": "general",
            "reasoning_mode": "none",
            "quality_focus": "none",
            "metadata": {
                "web_routing_guard": {
                    "performed": True,
                    "reason": ("fresh_public_" "evidence_required"),
                }
            },
        }
