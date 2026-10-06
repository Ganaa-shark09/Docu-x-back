import os
import re


class ExternalConversationContextService:
    """
    Bounded same-conversation context for External AI.

    This service never crosses conversation boundaries and contains
    no person-, publisher-, document-, or query-specific knowledge.

    Modes:
      routing:
          Small recent context for intent/reference resolution.

      answer:
          Larger recent context for conversational follow-ups.

      summary:
          Broad conversation context for "summarize our chat",
          "what have we discussed", etc.
    """

    SUMMARY_PATTERNS = (
        re.compile(
            (
                r"\b("
                r"summarize|summarise|summary|recap|review"
                r")\b"
                r".{0,80}"
                r"\b("
                r"chat|conversation|discussion|thread"
                r")\b"
            ),
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            (
                r"\b("
                r"what|which"
                r")\b"
                r".{0,60}"
                r"\b("
                r"discussed|covered|talked\s+about"
                r")\b"
            ),
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            (
                r"\bcatch\s+me\s+up\b"
                r".{0,80}"
                r"\b("
                r"chat|conversation|discussion|so\s+far"
                r")\b"
            ),
            re.IGNORECASE | re.DOTALL,
        ),
    )

    MODE_CONFIG = {
        "routing": {
            "max_messages_env": "EXTERNAL_AI_ROUTING_HISTORY_MAX_MESSAGES",
            "max_messages": 10,
            "per_message_env": "EXTERNAL_AI_ROUTING_HISTORY_PER_MESSAGE_CHARS",
            "per_message_chars": 900,
            "total_chars_env": "EXTERNAL_AI_ROUTING_HISTORY_TOTAL_CHARS",
            "total_chars": 6000,
        },
        "answer": {
            "max_messages_env": "EXTERNAL_AI_ANSWER_HISTORY_MAX_MESSAGES",
            "max_messages": 20,
            "per_message_env": "EXTERNAL_AI_ANSWER_HISTORY_PER_MESSAGE_CHARS",
            "per_message_chars": 1600,
            "total_chars_env": "EXTERNAL_AI_ANSWER_HISTORY_TOTAL_CHARS",
            "total_chars": 16000,
        },
        "summary": {
            "max_messages_env": "EXTERNAL_AI_SUMMARY_HISTORY_MAX_MESSAGES",
            "max_messages": 80,
            "per_message_env": "EXTERNAL_AI_SUMMARY_HISTORY_PER_MESSAGE_CHARS",
            "per_message_chars": 1600,
            "total_chars_env": "EXTERNAL_AI_SUMMARY_HISTORY_TOTAL_CHARS",
            "total_chars": 24000,
        },
    }

    @classmethod
    def is_summary_request(
        cls,
        message,
    ):
        value = " ".join(str(message or "").split())

        if not value:
            return False

        return any(pattern.search(value) for pattern in cls.SUMMARY_PATTERNS)

    @classmethod
    def build_history_messages(
        cls,
        conversation,
        exclude_message_uuid=None,
        mode="answer",
    ):
        if conversation is None:
            return []

        config = cls._config(mode)

        queryset = conversation.messages.filter(
            is_deleted=False,
            role__in=[
                "user",
                "assistant",
            ],
        )

        if exclude_message_uuid:
            queryset = queryset.exclude(uuid=exclude_message_uuid)

        #
        # Query newest first so the database only needs to return
        # the configured bounded number of messages.
        #
        newest = list(queryset.order_by("-created_at")[: config["max_messages"]])

        rows = []

        for message in reversed(newest):
            metadata = message.metadata or {}

            #
            # Failed clarification answers must not poison future
            # semantic routing.
            #
            if (
                mode == "routing"
                and message.role == "assistant"
                and metadata.get("semantic_action") == "clarify"
            ):
                continue

            rows.append(
                {
                    "role": message.role,
                    "content": str(message.content or ""),
                    "metadata": metadata,
                }
            )

        return cls.bound_history_rows(
            rows=rows,
            mode=mode,
        )

    @classmethod
    def bound_history_rows(
        cls,
        rows,
        mode="answer",
    ):
        config = cls._config(mode)

        normalized = []

        for raw in rows or []:
            role = str(raw.get("role") or "").strip().lower()

            content = " ".join(str(raw.get("content") or "").split())

            if (
                role
                not in {
                    "user",
                    "assistant",
                }
                or not content
            ):
                continue

            if (
                mode == "routing"
                and role == "assistant"
                and (raw.get("metadata") or {}).get("semantic_action") == "clarify"
            ):
                continue

            normalized.append(
                {
                    "role": role,
                    "content": content,
                    "metadata": raw.get("metadata") or {},
                }
            )

        normalized = normalized[-config["max_messages"] :]

        if not normalized:
            return []

        if mode == "summary":
            #
            # For a conversation summary it is better to preserve
            # representation across the whole bounded conversation
            # instead of spending the entire budget on the newest
            # few long messages.
            #
            adaptive_limit = max(
                240,
                int(config["total_chars"] / len(normalized)),
            )

            adaptive_limit = min(
                adaptive_limit,
                config["per_message_chars"],
            )

            result = []

            for row in normalized:
                result.append(
                    {
                        "role": row["role"],
                        "content": cls._clip(
                            row["content"],
                            adaptive_limit,
                        ),
                    }
                )

            return result

        #
        # Normal follow-up/routing memory should prioritise the
        # newest turns when the character budget is exceeded.
        #
        remaining = config["total_chars"]

        selected = []

        for row in reversed(normalized):
            if remaining <= 0:
                break

            allowed = min(
                config["per_message_chars"],
                remaining,
            )

            content = cls._clip(
                row["content"],
                allowed,
            )

            if not content:
                continue

            selected.append(
                {
                    "role": row["role"],
                    "content": content,
                }
            )

            remaining -= len(content)

        return list(reversed(selected))

    @classmethod
    def _config(
        cls,
        mode,
    ):
        mode = str(mode or "answer").strip().lower()

        if mode not in cls.MODE_CONFIG:
            mode = "answer"

        defaults = cls.MODE_CONFIG[mode]

        return {
            "max_messages": max(
                1,
                int(
                    os.getenv(
                        defaults["max_messages_env"],
                        str(defaults["max_messages"]),
                    )
                ),
            ),
            "per_message_chars": max(
                200,
                int(
                    os.getenv(
                        defaults["per_message_env"],
                        str(defaults["per_message_chars"]),
                    )
                ),
            ),
            "total_chars": max(
                1000,
                int(
                    os.getenv(
                        defaults["total_chars_env"],
                        str(defaults["total_chars"]),
                    )
                ),
            ),
        }

    @staticmethod
    def _clip(
        value,
        limit,
    ):
        value = str(value or "").strip()

        if not value:
            return ""

        if len(value) <= limit:
            return value

        clipped = value[:limit]

        sentence_end = max(
            clipped.rfind(". "),
            clipped.rfind("? "),
            clipped.rfind("! "),
        )

        if sentence_end >= int(limit * 0.55):
            return clipped[: sentence_end + 1].strip()

        return clipped.rstrip() + "…"
