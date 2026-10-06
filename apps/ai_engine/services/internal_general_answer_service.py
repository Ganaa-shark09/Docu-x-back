import logging
import os
import time

import requests

from apps.ai_engine.services.external_ai_model_policy import ExternalAIModelPolicy

from apps.ai_engine.services.internal_conversation_memory_service import (
    InternalConversationMemoryService,
)


logger = logging.getLogger(__name__)


class InternalGeneralAnswerService:
    """Generate document-independent Internal AI responses without phrase maps."""

    @classmethod
    def generate(cls, message, conversation=None, current_user_message_uuid=None):
        model = os.getenv(
            "OLLAMA_INTERNAL_CONVERSATION_MODEL",
            ExternalAIModelPolicy.fast_text_model(),
        )
        base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
        timeout = int(os.getenv("OLLAMA_INTERNAL_CONVERSATION_TIMEOUT_SECONDS", "90"))
        max_tokens = int(os.getenv("OLLAMA_INTERNAL_CONVERSATION_MAX_TOKENS", "220"))
        num_ctx = int(os.getenv("OLLAMA_INTERNAL_CONVERSATION_NUM_CTX", "4096"))

        messages = [
            {
                "role": "system",
                "content": (
                    "You are DocuX Internal AI, a private company-document assistant. "
                    "No sufficiently relevant document context is available for this turn. "
                    "Respond naturally to casual conversation and questions about using the "
                    "assistant. If the user asks for company-specific facts, policies, records, "
                    "numbers, or document content, do not invent an answer; explain that relevant "
                    "document context was not found and ask them to select a document or rephrase. "
                    "Do not claim that you inspected documents when no relevant context was supplied. "
                    "Keep ordinary conversational responses concise."
                ),
            }
        ]
        messages.extend(
            cls._history(
                conversation=conversation,
                exclude_message_uuid=current_user_message_uuid,
            )
        )
        messages.append({"role": "user", "content": str(message or "")})

        started_at = time.perf_counter()
        try:
            response = requests.post(
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "think": False,
                    "keep_alive": os.getenv("OLLAMA_REQUEST_KEEP_ALIVE", "30m"),
                    "messages": messages,
                    "options": {
                        "temperature": float(
                            os.getenv("OLLAMA_INTERNAL_CONVERSATION_TEMPERATURE", "0.2")
                        ),
                        "num_predict": max_tokens,
                        "num_ctx": num_ctx,
                    },
                },
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            answer = (data.get("message", {}).get("content") or "").strip()
            if not answer:
                raise ValueError("Ollama returned an empty Internal AI response.")

            prompt_tokens = int(data.get("prompt_eval_count", 0) or 0)
            completion_tokens = int(data.get("eval_count", 0) or 0)
            wall_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.info(
                "internal_ai_general_complete model=%s wall_ms=%s prompt_tokens=%s completion_tokens=%s",
                model,
                wall_duration_ms,
                prompt_tokens,
                completion_tokens,
            )
            return {
                "answer": answer,
                "provider": "ollama",
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "metadata": {
                    "document_context_used": False,
                    "wall_duration_ms": wall_duration_ms,
                },
            }
        except Exception as exc:
            wall_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.warning(
                "internal_ai_general_failed model=%s wall_ms=%s error=%s",
                model,
                wall_duration_ms,
                exc,
            )
            answer = (
                "I couldn't generate a conversational response right now. "
                "Please try again, or select a company document if your question depends on one."
            )
            return {
                "answer": answer,
                "provider": "system",
                "model": "internal_general_fallback",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "metadata": {
                    "document_context_used": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "wall_duration_ms": wall_duration_ms,
                },
            }

    @staticmethod
    def _history(conversation, exclude_message_uuid=None):
        if conversation is None:
            return []

        return InternalConversationMemoryService.build_history_messages(
            conversation=conversation,
            user=conversation.user,
            exclude_message_uuid=(exclude_message_uuid),
            mode="answer",
        )
