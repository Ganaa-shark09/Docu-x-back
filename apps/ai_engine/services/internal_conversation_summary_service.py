import logging
import os
import time

import requests

from apps.ai_engine.services.external_ai_model_policy import (
    ExternalAIModelPolicy,
)

from apps.ai_engine.services.internal_conversation_memory_service import (
    InternalConversationMemoryService,
)


logger = logging.getLogger(__name__)


class InternalConversationSummaryService:
    """
    Local-only summary of the current Internal AI conversation.

    Permission-aware history is prepared before anything is sent
    to the local model.
    """

    @classmethod
    def generate(
        cls,
        user,
        conversation,
        current_user_message_uuid=None,
        message="Summarize our conversation.",
    ):
        history = InternalConversationMemoryService.build_history_messages(
            conversation=conversation,
            user=user,
            exclude_message_uuid=current_user_message_uuid,
            mode="summary",
        )

        if not history:
            return cls._fallback(history=[])

        transcript = cls._transcript(history)

        model = (
            os.getenv(
                "OLLAMA_INTERNAL_MEMORY_MODEL",
                "",
            ).strip()
            or os.getenv(
                "OLLAMA_INTERNAL_CONVERSATION_MODEL",
                "",
            ).strip()
            or ExternalAIModelPolicy.fast_text_model()
        )

        base_url = os.getenv(
            "OLLAMA_BASE_URL",
            "http://ollama:11434",
        ).rstrip("/")

        timeout = int(
            os.getenv(
                ("OLLAMA_INTERNAL_MEMORY_" "TIMEOUT_SECONDS"),
                "120",
            )
        )

        num_ctx = int(
            os.getenv(
                ("OLLAMA_INTERNAL_MEMORY_" "NUM_CTX"),
                "8192",
            )
        )

        max_tokens = int(
            os.getenv(
                ("OLLAMA_INTERNAL_MEMORY_" "MAX_TOKENS"),
                "700",
            )
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are DocuX Internal AI. "
                    "The user is asking about this same private "
                    "Internal AI conversation. Summarize only the "
                    "conversation history supplied below. Cover "
                    "the major topics across the conversation, not "
                    "only the last topic. Previous assistant messages "
                    "describe what DocuX previously answered; they are "
                    "not automatically authoritative company evidence. "
                    "If a history entry says earlier context was omitted "
                    "because access is no longer available, do not infer "
                    "or reconstruct that hidden information. Do not use "
                    "outside knowledge. Do not perform web research."
                ),
            },
            {
                "role": "user",
                "content": (
                    "CONVERSATION HISTORY:\n\n"
                    f"{transcript}\n\n"
                    "CURRENT REQUEST:\n"
                    f"{message}"
                ),
            },
        ]

        started_at = time.perf_counter()

        try:
            response = requests.post(
                (f"{base_url}" "/api/chat"),
                json={
                    "model": model,
                    "stream": False,
                    "think": False,
                    "keep_alive": os.getenv(
                        ("OLLAMA_REQUEST_" "KEEP_ALIVE"),
                        "30m",
                    ),
                    "messages": messages,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": max_tokens,
                        "num_ctx": num_ctx,
                    },
                },
                timeout=timeout,
            )

            response.raise_for_status()

            data = response.json()

            answer = str((data.get("message") or {}).get("content") or "").strip()

            if not answer:
                raise ValueError(
                    ("Ollama returned an " "empty conversation " "summary.")
                )

            prompt_tokens = int(
                data.get(
                    "prompt_eval_count",
                    0,
                )
                or 0
            )

            completion_tokens = int(
                data.get(
                    "eval_count",
                    0,
                )
                or 0
            )

            wall_ms = round(
                (time.perf_counter() - started_at) * 1000,
                2,
            )

            logger.info(
                (
                    "internal_ai_memory_summary_complete "
                    "model=%s messages=%s wall_ms=%s"
                ),
                model,
                len(history),
                wall_ms,
            )

            return {
                "answer": answer,
                "provider": "ollama",
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": (prompt_tokens + completion_tokens),
                "metadata": {
                    "conversation_memory": True,
                    "local_only": True,
                    "history_message_count": len(history),
                    "wall_duration_ms": wall_ms,
                },
            }

        except Exception as exc:
            logger.warning(
                ("internal_ai_memory_summary_failed " "model=%s error=%s"),
                model,
                exc,
            )

            return cls._fallback(
                history=history,
                failure_metadata={
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
            )

    @classmethod
    def _fallback(
        cls,
        history,
        failure_metadata=None,
    ):
        if not history:
            answer = (
                "There isn't enough earlier "
                "conversation in this Internal AI "
                "chat to summarize yet."
            )

        else:
            lines = [("Here is a recap of our " "Internal AI conversation:")]

            for item in history[-24:]:
                prefix = "You" if item.get("role") == "user" else "DocuX"

                content = InternalConversationMemoryService._clip(
                    item.get("content") or "",
                    320,
                )

                lines.append((f"- {prefix}: " f"{content}"))

            answer = "\n".join(lines)

        return {
            "answer": answer,
            "provider": ("internal_memory_" "fallback"),
            "model": ("deterministic_" "conversation_summary"),
            "prompt_tokens": 0,
            "completion_tokens": len(answer.split()),
            "total_tokens": len(answer.split()),
            "metadata": {
                "conversation_memory": True,
                "local_only": True,
                "fallback_used": True,
                "history_message_count": len(history),
                "failure_metadata": failure_metadata or {},
            },
        }

    @staticmethod
    def _transcript(
        history,
    ):
        output = []

        for index, item in enumerate(
            history,
            start=1,
        ):
            role = "USER" if item.get("role") == "user" else "DOCUX"

            output.append(
                (f"[Turn {index} - " f"{role}]\n" f"{item.get('content') or ''}")
            )

        return "\n\n".join(output)
