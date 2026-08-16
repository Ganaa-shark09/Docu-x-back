import logging
import os
import time

import requests

from apps.ai_engine.services.external_ai_model_policy import ExternalAIModelPolicy

logger = logging.getLogger(__name__)


class ExternalGeneralAnswerService:
    @classmethod
    def generate(
        cls,
        message,
        intent="general_question",
        conversation=None,
        current_user_message_uuid=None,
        model=None,
        reasoning_mode="standard",
        answer_profile="general",
        quality_requirements=None,
        allow_specialized_fallback=True,
    ):
        answer_profile = (answer_profile or "general").strip().lower()
        quality_requirements = [
            str(item).strip()[:240]
            for item in (quality_requirements or [])[:6]
            if str(item or "").strip()
        ]
        if answer_profile not in ExternalAIModelPolicy.VALID_DIRECT_PROFILES:
            answer_profile = ExternalAIModelPolicy.PROFILE_GENERAL

        model = model or ExternalAIModelPolicy.direct_model(
            answer_tier="quality",
            answer_profile=answer_profile,
        )
        base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
        is_fast_model = model == ExternalAIModelPolicy.fast_text_model()
        is_coding_model = model == ExternalAIModelPolicy.coding_model()
        reasoning_mode = (reasoning_mode or "standard").strip().lower()
        if reasoning_mode not in {"none", "standard", "deep"}:
            reasoning_mode = "standard"
        if is_fast_model:
            reasoning_mode = "none"
        elif is_coding_model:
            # qwen2.5-coder does not use Qwen3's thinking channel. Keep the
            # specialized coding path single-pass and validate through prompt policy.
            reasoning_mode = "standard"

        thinking_mode = cls.resolve_thinking_mode(
            is_fast_model=is_fast_model,
            reasoning_mode=reasoning_mode,
            supports_thinking=not is_coding_model,
        )
        deep_reasoning = bool(thinking_mode is not False)

        timeout = int(
            os.getenv(
                (
                    "OLLAMA_FAST_ANSWER_TIMEOUT_SECONDS"
                    if is_fast_model
                    else (
                        "OLLAMA_CODING_ANSWER_TIMEOUT_SECONDS"
                        if is_coding_model
                        else (
                            "OLLAMA_QUALITY_REASONING_TIMEOUT_SECONDS"
                            if deep_reasoning
                            else "OLLAMA_QUALITY_ANSWER_TIMEOUT_SECONDS"
                        )
                    )
                ),
                os.getenv(
                    "OLLAMA_GENERAL_TIMEOUT_SECONDS",
                    (
                        "120"
                        if is_fast_model
                        else (
                            "420"
                            if is_coding_model
                            else ("600" if deep_reasoning else "420")
                        )
                    ),
                ),
            )
        )

        max_tokens = int(
            os.getenv(
                "OLLAMA_FAST_ANSWER_MAX_TOKENS"
                if is_fast_model
                else (
                    "OLLAMA_CODING_ANSWER_MAX_TOKENS"
                    if is_coding_model
                    else (
                        "OLLAMA_QUALITY_REASONING_MAX_TOKENS"
                        if deep_reasoning
                        else "OLLAMA_QUALITY_ANSWER_MAX_TOKENS"
                    )
                ),
                "320"
                if is_fast_model
                else (
                    "1400"
                    if is_coding_model
                    else (
                        "1800"
                        if deep_reasoning
                        else os.getenv("OLLAMA_DEFAULT_MAX_TOKENS", "1400")
                    )
                ),
            )
        )
        num_ctx = int(
            os.getenv(
                "OLLAMA_FAST_ANSWER_NUM_CTX"
                if is_fast_model
                else (
                    "OLLAMA_CODING_ANSWER_NUM_CTX"
                    if is_coding_model
                    else "OLLAMA_QUALITY_ANSWER_NUM_CTX"
                ),
                "4096"
                if is_fast_model
                else os.getenv(
                    "OLLAMA_GENERAL_NUM_CTX",
                    os.getenv("OLLAMA_NUM_CTX", "8192"),
                ),
            )
        )

        system_prompt = cls.build_system_prompt(
            intent=intent,
            quality_mode=not is_fast_model,
            deep_reasoning=deep_reasoning,
            answer_profile=answer_profile,
            quality_requirements=quality_requirements,
        )
        history_messages = cls.build_history_messages(
            conversation=conversation,
            exclude_message_uuid=current_user_message_uuid,
        )

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history_messages[-6:])
        messages.append({"role": "user", "content": message})

        started_at = time.perf_counter()

        try:
            response = requests.post(
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "think": thinking_mode,
                    "keep_alive": os.getenv("OLLAMA_REQUEST_KEEP_ALIVE", "30m"),
                    "messages": messages,
                    "options": {
                        "temperature": float(
                            os.getenv(
                                "OLLAMA_FAST_ANSWER_TEMPERATURE"
                                if is_fast_model
                                else (
                                    "OLLAMA_CODING_ANSWER_TEMPERATURE"
                                    if is_coding_model
                                    else "OLLAMA_QUALITY_ANSWER_TEMPERATURE"
                                ),
                                os.getenv("OLLAMA_TEMPERATURE", "0.2")
                                if is_fast_model
                                else "0.1",
                            )
                        ),
                        "num_predict": max_tokens,
                        "num_ctx": num_ctx,
                    },
                },
                timeout=timeout,
            )
            response.raise_for_status()

            data = response.json()
            message_data = data.get("message", {})
            answer = message_data.get("content", "").strip()
            thinking_text = message_data.get("thinking", "") or ""
            prompt_tokens = int(data.get("prompt_eval_count", 0) or 0)
            completion_tokens = int(data.get("eval_count", 0) or 0)
            done_reason = (data.get("done_reason") or "").strip()
            truncated_by_limit = bool(
                done_reason == "length"
                or (
                    max_tokens > 0
                    and completion_tokens >= max_tokens
                    and done_reason not in {"stop", "completed"}
                )
            )
            wall_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

            logger.info(
                "external_ai_general_complete model=%s tier=%s intent=%s wall_ms=%s "
                "prompt_tokens=%s completion_tokens=%s max_tokens=%s num_ctx=%s "
                "done_reason=%s truncated=%s answer_profile=%s reasoning_mode=%s thinking=%s thinking_chars=%s",
                model,
                "fast" if is_fast_model else "quality",
                intent,
                wall_duration_ms,
                prompt_tokens,
                completion_tokens,
                max_tokens,
                num_ctx,
                done_reason or "unknown",
                truncated_by_limit,
                answer_profile,
                reasoning_mode,
                thinking_mode,
                len(thinking_text),
            )

            return {
                "answer": cls.clean_answer(answer),
                "provider": "ollama",
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "metadata": {
                    "intent": intent,
                    "external_general_answer": True,
                    "used_conversation_history": bool(history_messages),
                    "wall_duration_ms": wall_duration_ms,
                    "load_duration": data.get("load_duration", 0),
                    "prompt_eval_duration": data.get("prompt_eval_duration", 0),
                    "eval_duration": data.get("eval_duration", 0),
                    "answer_tier": "fast" if is_fast_model else "quality",
                    "answer_profile": answer_profile,
                    "quality_requirements": quality_requirements,
                    "specialized_model": bool(is_coding_model),
                    "done_reason": done_reason,
                    "truncated_by_limit": truncated_by_limit,
                    "max_tokens": max_tokens,
                    "num_ctx": num_ctx,
                    "reasoning_mode": reasoning_mode,
                    "thinking_enabled": thinking_mode is not False,
                    "thinking_mode": thinking_mode,
                    "thinking_chars": len(thinking_text),
                },
            }

        except Exception as exc:
            wall_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.warning(
                "external_ai_general_failed model=%s intent=%s wall_ms=%s error=%s",
                model,
                intent,
                wall_duration_ms,
                exc,
            )

            if (
                allow_specialized_fallback
                and is_coding_model
                and model != ExternalAIModelPolicy.quality_text_model()
            ):
                logger.warning(
                    "external_ai_coding_model_fallback from_model=%s to_model=%s",
                    model,
                    ExternalAIModelPolicy.quality_text_model(),
                )
                fallback = cls.generate(
                    message=message,
                    intent=intent,
                    conversation=conversation,
                    current_user_message_uuid=current_user_message_uuid,
                    model=ExternalAIModelPolicy.quality_text_model(),
                    reasoning_mode="standard",
                    answer_profile=answer_profile,
                    quality_requirements=quality_requirements,
                    allow_specialized_fallback=False,
                )
                fallback.setdefault("metadata", {})[
                    "specialized_model_fallback_from"
                ] = model
                return fallback

            return {
                "answer": cls.fallback_answer(intent=intent),
                "provider": "system",
                "model": "external_general_fallback",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "metadata": {
                    "intent": intent,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "wall_duration_ms": wall_duration_ms,
                    "external_general_answer": True,
                    "answer_profile": answer_profile,
                },
            }

    @staticmethod
    def resolve_thinking_mode(is_fast_model, reasoning_mode, supports_thinking=True):
        if not supports_thinking or is_fast_model or reasoning_mode != "deep":
            return False

        raw = os.getenv("OLLAMA_QUALITY_THINKING_MODE", "true").strip().lower()
        if raw in {"false", "0", "no", "off"}:
            return False
        if raw in {"true", "1", "yes", "on"}:
            return True
        if raw in {"low", "medium", "high", "max"}:
            return raw
        return True

    @staticmethod
    def build_system_prompt(
        intent,
        quality_mode=False,
        deep_reasoning=False,
        answer_profile="general",
        quality_requirements=None,
    ):
        quality_contract = ""
        quality_requirements = quality_requirements or []
        if quality_mode:
            coding_contract = ""
            if answer_profile == "coding":
                task_checklist = ""
                if quality_requirements:
                    task_checklist = (
                        "\nTask-specific acceptance criteria from the semantic planner:\n"
                        + "\n".join(f"- {item}" for item in quality_requirements)
                        + "\nTreat these as acceptance criteria, not suggestions. Before returning the final answer, ensure the code and explanation satisfy each item.\n"
                    )
                coding_contract = (
                    "\nCoding-profile requirements:\n"
                    "- Treat the requested implementation as production software, not illustrative pseudocode.\n"
                    "- Before finalizing, verify that every concurrency/atomicity/thread-safety claim is actually provided by the code.\n"
                    "- Remove unused locks, variables, imports, keys, and dead logic; do not explain mechanisms that the code does not use.\n"
                    "- Distinguish fixed-window, sliding-window, token-bucket, and leaky-bucket semantics accurately when relevant.\n"
                    "- Prefer server-side atomic primitives/scripts/transactions when a check-and-update sequence must be indivisible.\n"
                    "- Make failure policy explicit where relevant (for example fail-open vs fail-closed), and include practical timeouts/observability.\n"
                    "- Verify third-party API signatures/argument order used in the code; do not guess library call semantics.\n"
                    "- Ensure read-only inspection helpers do not mutate the state they are reporting unless explicitly documented.\n"
                    "- Mentally review the final code and explanation together for contradictions before returning the answer.\n"
                    f"{task_checklist}"
                )

            deep_contract = ""
            if deep_reasoning:
                deep_contract = (
                    "- Use extended reasoning internally to validate the solution before the final answer.\n"
                    "- Check invariants, race conditions, edge cases, failure paths, and whether the implementation actually matches every guarantee it claims.\n"
                    "- Keep the reasoning private; return only the final answer to the user.\n"
                )
            quality_contract = (
                "\nQuality-mode requirements:\n"
                "- Prioritize correctness, internal consistency, and completeness.\n"
                "- For code, provide executable code unless the user explicitly asks for pseudocode.\n"
                "- Check names, types, control flow, API usage, and examples for obvious defects before answering.\n"
                "- Do not claim an operation is atomic, transactional, thread-safe, or race-free unless the implementation actually provides that guarantee.\n"
                "- For concurrent or distributed state changes, use an appropriate atomic primitive/transaction/script when required and explain the guarantee.\n"
                "- Consider timeouts, retries, idempotency, partial failure, availability, cleanup, and observability when they materially apply.\n"
                "- Finish every requested section; do not end mid-sentence or mid-list.\n"
                "- Be detailed when requested, but avoid repetition so the complete answer fits within the response budget.\n"
                f"{coding_contract}"
                f"{deep_contract}"
            )

        return (
            "You are DocuX External AI, the external/personal AI workspace inside "
            "the DocuX application.\n\n"
            "Core behavior:\n"
            "- Answer naturally and helpfully.\n"
            "- Do not reveal hidden reasoning.\n"
            "- Do not mention indexed company documents unless document context is explicitly provided.\n"
            "- Do not claim that you searched the web unless web context is explicitly provided.\n"
            "- Do not invent sources.\n"
            "- Present yourself as DocuX External AI. Do not identify yourself as "
            "Qwen, Alibaba Cloud, OpenAI, or another underlying model/vendor unless "
            "the user explicitly asks for technical runtime/model details.\n"
            "- For greetings or personal questions about yourself, answer naturally as DocuX External AI.\n"
            "- For capability questions, explain what you can help with in DocuX External AI.\n"
            "- For unclear questions, ask a short clarification.\n"
            "- Match answer depth to the task. Simple questions can be concise, but detailed explanations, code, comparisons, analysis, and multi-step requests should be complete and should not omit material steps or points.\n"
            "- When the user asks for detail, prioritize completeness over brevity while avoiding needless repetition.\n\n"
            "Capabilities you can mention:\n"
            "- General questions\n"
            "- Web-backed answers when web search is enabled\n"
            "- Uploaded file and image analysis\n"
            "- PDF, DOCX, XLSX, TXT, CSV, and image-based Q&A\n"
            "- Screenshot/OCR-based understanding\n"
            "- Continuing a chat with previously attached external files\n\n"
            f"Current classified intent: {intent}\n"
            f"Current answer profile: {answer_profile}\n"
            f"{quality_contract}"
        )

    @staticmethod
    def build_history_messages(conversation, exclude_message_uuid=None):
        if not conversation:
            return []

        queryset = conversation.messages.filter(is_deleted=False)

        if exclude_message_uuid:
            queryset = queryset.exclude(uuid=exclude_message_uuid)

        queryset = queryset.order_by("-created_at")[:6]

        messages = []

        for message in reversed(list(queryset)):
            if message.role not in ["user", "assistant"]:
                continue

            messages.append(
                {
                    "role": message.role,
                    "content": message.content[:1400],
                }
            )

        return messages

    @staticmethod
    def clean_answer(answer):
        if not answer:
            return (
                "I am DocuX External AI. I can help with general questions, web-backed "
                "answers, and uploaded files or images in this external workspace."
            )

        blocked_phrases = [
            "indexed company documents",
            "company documents",
            "provided document context",
        ]

        lowered = answer.lower()

        if any(phrase in lowered for phrase in blocked_phrases):
            return (
                "I am DocuX External AI. I can help with general questions, web-backed "
                "answers, and uploaded files or images in this external workspace."
            )

        return answer

    @staticmethod
    def fallback_answer(intent):
        if intent in ["smalltalk", "assistant_identity"]:
            return (
                "I am DocuX External AI. I am running inside your DocuX application and "
                "I am ready to help with general questions, web-backed answers, and "
                "uploaded files or images."
            )

        return (
            "I am ready to help. You can ask a general question, enable web search, or "
            "upload files/images and ask questions about them."
        )
