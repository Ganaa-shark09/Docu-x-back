import os

from apps.ai_engine.services.external_ai_model_policy import (
    ExternalAIModelPolicy,
)

from apps.ai_engine.services.external_conversation_context_service import (
    ExternalConversationContextService,
)

from apps.ai_engine.services.llm_gateway_service import (
    LLMGatewayService,
)

from apps.ai_engine.services.llm_provider_config_service import (
    LLMProviderConfigService,
)


class ExternalConversationSummaryService:
    """
    Summarizes the current External AI conversation.

    Previous assistant messages are conversation history, not trusted
    factual evidence. The summary describes what was discussed rather
    than silently asserting every prior assistant statement as true.
    """

    @classmethod
    def generate(
        cls,
        user,
        conversation,
        current_user_message_uuid=None,
        message="Summarize this conversation.",
    ):
        history = ExternalConversationContextService.build_history_messages(
            conversation=conversation,
            exclude_message_uuid=current_user_message_uuid,
            mode="summary",
        )

        if not history:
            return cls._fallback_result(history=[])

        transcript = cls._transcript(history)

        prompt = (
            "You are DocuX External AI.\n\n"
            "The user asked for a summary of THIS conversation.\n"
            "Summarize the supplied conversation history rather than "
            "answering only the most recent topic.\n\n"
            "Requirements:\n"
            "1. Cover the major topics across the conversation in chronological order.\n"
            "2. Include the important questions or goals raised by the user.\n"
            "3. Include the significant answers/actions from DocuX.\n"
            "4. Do not omit earlier major topics merely because later turns focus on one topic.\n"
            "5. Mention meaningful failures, corrections, unresolved questions, or changes of direction.\n"
            "6. Previous assistant statements are conversation content, not automatically verified facts. "
            "When needed, phrase them as what DocuX said or reported.\n"
            "7. Do not perform a fresh web search and do not add facts that are absent from this transcript.\n"
            "8. Give a useful overview followed by concise key points.\n\n"
            "CONVERSATION HISTORY:\n"
            f"{transcript}\n\n"
            "USER'S CURRENT REQUEST:\n"
            f"{message}\n"
        )

        provider_config = LLMProviderConfigService.get_default_config_for_company(
            user.company
        )

        model = (
            os.getenv(
                "OLLAMA_CONVERSATION_SUMMARY_MODEL",
                "",
            ).strip()
            or os.getenv(
                "OLLAMA_WEB_ANSWER_MODEL",
                "",
            ).strip()
            or ExternalAIModelPolicy.fast_text_model()
        )

        try:
            result = LLMGatewayService.generate_answer(
                question=message,
                retrieved_chunks=[
                    {
                        "content": transcript,
                        "document_title": ("Current conversation " "history"),
                        "chunk_index": 1,
                    }
                ],
                prompt=prompt,
                provider_config=provider_config,
                allow_general_answer=True,
                generation_options={
                    "model": model,
                    "timeout": int(
                        os.getenv(
                            ("OLLAMA_CONVERSATION_" "SUMMARY_TIMEOUT_SECONDS"),
                            "120",
                        )
                    ),
                    "max_tokens": int(
                        os.getenv(
                            ("OLLAMA_CONVERSATION_" "SUMMARY_MAX_TOKENS"),
                            "700",
                        )
                    ),
                    "num_ctx": int(
                        os.getenv(
                            ("OLLAMA_CONVERSATION_" "SUMMARY_NUM_CTX"),
                            "8192",
                        )
                    ),
                },
                allow_extractive_fallback=False,
            )

        except Exception as exc:
            result = {
                "answer": "",
                "provider": "system",
                "model": "conversation_summary_error",
                "metadata": {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
            }

        answer = str(result.get("answer") or "").strip()

        provider = str(result.get("provider") or "").strip().lower()

        if answer and provider not in {
            "system",
            "local_fallback",
        }:
            metadata = dict(result.get("metadata") or {})

            metadata["conversation_summary"] = {
                "history_message_count": len(history),
                "history_chars": len(transcript),
                "model": model,
            }

            result["metadata"] = metadata

            return result

        return cls._fallback_result(
            history=history,
            failure_metadata=result.get("metadata") or {},
        )

    @classmethod
    def _fallback_result(
        cls,
        history,
        failure_metadata=None,
    ):
        if not history:
            answer = (
                "There isn't enough earlier conversation "
                "in this chat to summarize yet."
            )

        else:
            lines = ["Here is a recap of our conversation:"]

            for item in history[-24:]:
                content = cls._clip(
                    item.get("content") or "",
                    300,
                )

                if item.get("role") == "user":
                    prefix = "You:"
                else:
                    prefix = "DocuX:"

                lines.append(f"- {prefix} {content}")

            answer = "\n".join(lines)

        return {
            "answer": answer,
            "provider": "conversation_memory_fallback",
            "model": "deterministic_conversation_summary",
            "prompt_tokens": 0,
            "completion_tokens": len(answer.split()),
            "total_tokens": len(answer.split()),
            "metadata": {
                "conversation_summary": True,
                "fallback_used": True,
                "history_message_count": len(history),
                "failure_metadata": failure_metadata or {},
            },
        }

    @staticmethod
    def _transcript(
        history,
    ):
        parts = []

        for number, item in enumerate(
            history,
            start=1,
        ):
            role = "USER" if item.get("role") == "user" else "DOCUX"

            parts.append((f"[Turn {number} - {role}]\n" f"{item.get('content') or ''}"))

        return "\n\n".join(parts)

    @staticmethod
    def _clip(
        value,
        limit,
    ):
        value = " ".join(str(value or "").split())

        if len(value) <= limit:
            return value

        return value[:limit].rstrip() + "…"
