import os
import re

from apps.ai_engine.services.internal_document_scope_service import (
    InternalDocumentScopeService,
)


class InternalConversationMemoryService:
    """
    Permission-aware conversation memory for Internal AI.

    Security rules:
      - same conversation only
      - same user/company only
      - current document access is revalidated
      - old document-grounded assistant answers are hidden when
        their source documents are no longer accessible
      - conversation memory never grants document access
      - current retrieval/RBAC/firewall remain authoritative
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
                r".{0,80}"
                r"\b("
                r"discussed|covered|talked\s+about"
                r")\b"
            ),
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"\bcatch\s+me\s+up\b",
            re.IGNORECASE,
        ),
    )

    FOLLOWUP_PATTERN = re.compile(
        (
            r"(^|\b)("
            r"it|that|this|those|these|they|them|"
            r"earlier|previous|previously|above|same|again|"
            r"what\s+about|how\s+about|"
            r"and\s+what|and\s+the|"
            r"continue|continued|"
            r"go\s+back|"
            r"you\s+said|you\s+mentioned|"
            r"we\s+discussed"
            r")(\b|$)"
        ),
        re.IGNORECASE,
    )

    MODE_CONFIG = {
        "routing": {
            "max_messages_env": "INTERNAL_AI_ROUTING_HISTORY_MAX_MESSAGES",
            "max_messages": 10,
            "per_message_env": "INTERNAL_AI_ROUTING_HISTORY_PER_MESSAGE_CHARS",
            "per_message_chars": 900,
            "total_env": "INTERNAL_AI_ROUTING_HISTORY_TOTAL_CHARS",
            "total_chars": 6000,
        },
        "answer": {
            "max_messages_env": "INTERNAL_AI_ANSWER_HISTORY_MAX_MESSAGES",
            "max_messages": 20,
            "per_message_env": "INTERNAL_AI_ANSWER_HISTORY_PER_MESSAGE_CHARS",
            "per_message_chars": 1400,
            "total_env": "INTERNAL_AI_ANSWER_HISTORY_TOTAL_CHARS",
            "total_chars": 14000,
        },
        "summary": {
            "max_messages_env": "INTERNAL_AI_SUMMARY_HISTORY_MAX_MESSAGES",
            "max_messages": 80,
            "per_message_env": "INTERNAL_AI_SUMMARY_HISTORY_PER_MESSAGE_CHARS",
            "per_message_chars": 1600,
            "total_env": "INTERNAL_AI_SUMMARY_HISTORY_TOTAL_CHARS",
            "total_chars": 24000,
        },
    }

    REVOKED_CONTEXT_MESSAGE = (
        "[Earlier Internal AI answer omitted because one or more "
        "source documents are no longer accessible to the current user.]"
    )

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
        user,
        exclude_message_uuid=None,
        mode="answer",
    ):
        if conversation is None or user is None:
            return []

        if (
            getattr(
                conversation,
                "user_id",
                None,
            )
            != getattr(
                user,
                "id",
                None,
            )
            or getattr(
                conversation,
                "company_id",
                None,
            )
            != getattr(
                user,
                "company_id",
                None,
            )
            or str(
                getattr(
                    conversation,
                    "scope",
                    "",
                )
            ).lower()
            != "internal"
        ):
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

        newest = list(queryset.order_by("-created_at")[: config["max_messages"]])

        accessible_document_uuids = cls._accessible_document_uuids(user)

        rows = []

        for item in reversed(newest):
            rows.append(
                {
                    "role": item.role,
                    "content": item.content or "",
                    "retrieved_sources": item.retrieved_sources or [],
                    "metadata": item.metadata or {},
                }
            )

        return cls.prepare_rows(
            rows=rows,
            accessible_document_uuids=(accessible_document_uuids),
            mode=mode,
        )

    @classmethod
    def prepare_rows(
        cls,
        rows,
        accessible_document_uuids,
        mode="answer",
    ):
        config = cls._config(mode)

        accessible = {str(value) for value in (accessible_document_uuids or [])}

        normalized = []

        for raw in rows or []:
            role = str(raw.get("role") or "").strip().lower()

            if role not in {
                "user",
                "assistant",
            }:
                continue

            content = cls._normalize(raw.get("content"))

            if not content:
                continue

            if role == "assistant":
                source_ids = cls._source_document_uuids(raw)

                if source_ids and not source_ids.issubset(accessible):
                    content = cls.REVOKED_CONTEXT_MESSAGE

            normalized.append(
                {
                    "role": role,
                    "content": content,
                }
            )

        normalized = normalized[-config["max_messages"] :]

        if not normalized:
            return []

        if mode == "summary":
            per_message_limit = min(
                config["per_message_chars"],
                max(
                    240,
                    int(config["total_chars"] / len(normalized)),
                ),
            )

            return [
                {
                    "role": row["role"],
                    "content": cls._clip(
                        row["content"],
                        per_message_limit,
                    ),
                }
                for row in normalized
            ]

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
    def resolve_retrieval_query(
        cls,
        message,
        history,
    ):
        current = cls._normalize(message)

        if not current or not history:
            return current

        words = current.split()

        if len(words) > 18 or not cls.FOLLOWUP_PATTERN.search(current):
            return current

        previous_user_message = ""

        for item in reversed(history):
            if item.get("role") == "user":
                candidate = cls._normalize(item.get("content"))

                if candidate:
                    previous_user_message = candidate
                    break

        if not previous_user_message:
            return current

        previous_user_message = previous_user_message[:700]

        return (
            "Previous conversation topic: "
            f"{previous_user_message}\n"
            "Current follow-up question: "
            f"{current}"
        )

    @classmethod
    def _accessible_document_uuids(
        cls,
        user,
    ):
        queryset = InternalDocumentScopeService.get_ai_ready_queryset(user)

        return {
            str(value)
            for value in queryset.values_list(
                "uuid",
                flat=True,
            )
        }

    @classmethod
    def _source_document_uuids(
        cls,
        row,
    ):
        output = set()

        for source in row.get("retrieved_sources") or []:
            if not isinstance(
                source,
                dict,
            ):
                continue

            source_type = str(source.get("source_type") or "").lower()

            if source_type and source_type != "internal_document":
                continue

            value = source.get("document_uuid") or source.get("uuid")

            if value:
                output.add(str(value))

        metadata = row.get("metadata") or {}

        enforcement = metadata.get("firewall_enforcement") or {}

        for action in enforcement.get("policy_actions") or []:
            if not isinstance(
                action,
                dict,
            ):
                continue

            value = action.get("document_uuid")

            if value:
                output.add(str(value))

        return output

    @classmethod
    def _config(
        cls,
        mode,
    ):
        mode = str(mode or "answer").lower()

        if mode not in cls.MODE_CONFIG:
            mode = "answer"

        defaults = cls.MODE_CONFIG[mode]

        return {
            "max_messages": max(
                1,
                int(
                    os.getenv(
                        defaults["max_messages_env"],
                        defaults["max_messages"],
                    )
                ),
            ),
            "per_message_chars": max(
                200,
                int(
                    os.getenv(
                        defaults["per_message_env"],
                        defaults["per_message_chars"],
                    )
                ),
            ),
            "total_chars": max(
                1000,
                int(
                    os.getenv(
                        defaults["total_env"],
                        defaults["total_chars"],
                    )
                ),
            ),
        }

    @staticmethod
    def _normalize(
        value,
    ):
        return " ".join(str(value or "").split())

    @staticmethod
    def _clip(
        value,
        limit,
    ):
        value = str(value or "").strip()

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
