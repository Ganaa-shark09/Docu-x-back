import logging
import os
import re
import time

import requests

from apps.ai_engine.models import LLMProviderConfig
from apps.ai_engine.services.llm_cloud_providers import (
    ModelProviderUnavailable, GroqLLMProvider, GeminiLLMProvider, AnthropicLLMProvider,
)
from apps.ai_engine.services.llm_provider_security_service import (
    LLMProviderSecurityService,
)

logger = logging.getLogger(__name__)


class LocalFallbackLLMProvider:
    provider_name = "local_fallback"
    model_name = "local-extractive-rag"

    STOPWORDS = {
        "what",
        "which",
        "where",
        "when",
        "who",
        "why",
        "how",
        "is",
        "are",
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "in",
        "on",
        "to",
        "for",
        "with",
        "this",
        "that",
        "document",
        "invoice",
        "contract",
        "tell",
        "me",
        "show",
    }

    @classmethod
    def generate_answer(
        cls,
        question,
        retrieved_chunks,
        prompt=None,
        config=None,
        allow_general_answer=False,
        allow_extractive_fallback=True,
    ):
        if not retrieved_chunks:
            if allow_general_answer:
                return {
                    "answer": (
                        "I couldn't complete the AI request right now. Please try again."
                    ),
                    "provider": cls.provider_name,
                    "model": cls.model_name,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "metadata": {"fallback_reason": "general_generation_unavailable"},
                }

            return {
                "answer": (
                    "I could not find relevant information in the indexed company documents."
                ),
                "provider": cls.provider_name,
                "model": cls.model_name,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "metadata": {"fallback_reason": "no_retrieved_chunks"},
            }

        if not allow_extractive_fallback:
            answer = (
                "I found usable content in the attached document, but the AI "
                "synthesis request did not complete. Please retry the request."
            )
            return {
                "answer": answer,
                "provider": cls.provider_name,
                "model": cls.model_name,
                "prompt_tokens": len((prompt or "").split()),
                "completion_tokens": len(answer.split()),
                "total_tokens": len((prompt or "").split()) + len(answer.split()),
                "metadata": {"fallback_reason": "synthesis_generation_unavailable"},
            }

        best_sentences = cls._find_relevant_sentences(question, retrieved_chunks)

        if not best_sentences:
            # Never dump the top retrieved chunk just because an upstream model
            # call failed. Retrieval can be weak for very short or unrelated
            # queries, and exposing arbitrary source text creates both a poor
            # answer and an unnecessary data-disclosure risk.
            answer = (
                "I found document context, but I couldn't identify a sufficiently "
                "relevant passage to answer that question reliably. Please rephrase "
                "the question or select the document you want me to use."
            )
        else:
            answer = "\n".join(best_sentences[:4])

        return {
            "answer": answer,
            "provider": cls.provider_name,
            "model": cls.model_name,
            "prompt_tokens": len((prompt or "").split()),
            "completion_tokens": len(answer.split()),
            "total_tokens": len((prompt or "").split()) + len(answer.split()),
            "metadata": {},
        }

    @classmethod
    def _find_relevant_sentences(cls, question, retrieved_chunks):
        query_terms = {
            token.lower()
            for token in re.findall(r"[A-Za-z0-9_]+", question or "")
            if token.lower() not in cls.STOPWORDS and len(token) > 2
        }

        scored_sentences = []

        for chunk in retrieved_chunks:
            content = chunk.get("content", "")
            sentences = re.split(r"(?<=[.!?])\s+|\n+", content)

            for sentence in sentences:
                cleaned = sentence.strip()

                if not cleaned:
                    continue

                sentence_terms = {
                    token.lower() for token in re.findall(r"[A-Za-z0-9_]+", cleaned)
                }

                overlap = len(query_terms.intersection(sentence_terms))

                if overlap > 0:
                    scored_sentences.append(
                        {
                            "score": overlap,
                            "sentence": cleaned,
                            "document_title": chunk.get("document_title"),
                            "chunk_index": chunk.get("chunk_index"),
                        }
                    )

        scored_sentences.sort(key=lambda item: item["score"], reverse=True)

        return [
            f"{item['sentence']} (Source: {item['document_title']}, chunk {item['chunk_index']})"
            for item in scored_sentences
        ]


class OllamaLLMProvider:
    provider_name = "ollama"

    @classmethod
    def generate_answer(
        cls,
        question,
        retrieved_chunks,
        prompt=None,
        config=None,
        allow_general_answer=False,
        generation_options=None,
        allow_extractive_fallback=True,
    ):
        generation_options = generation_options or {}
        endpoint = (
            (config.endpoint_url or "http://ollama:11434").rstrip("/")
            if config
            else os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
        )
        model_name = generation_options.get("model") or (
            config.model_name
            if config and config.model_name
            else os.getenv("OLLAMA_DEFAULT_MODEL", "qwen3:8b")
        )
        temperature = (
            float(config.temperature)
            if config
            else float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))
        )
        max_tokens = (
            int(config.max_tokens)
            if config
            else int(os.getenv("OLLAMA_DEFAULT_MAX_TOKENS", "900"))
        )
        timeout = int(
            generation_options.get(
                "timeout",
                os.getenv("OLLAMA_ANSWER_TIMEOUT_SECONDS", "240"),
            )
        )
        max_tokens = int(generation_options.get("max_tokens", max_tokens))
        num_ctx = int(
            generation_options.get(
                "num_ctx",
                os.getenv("OLLAMA_NUM_CTX", "4096"),
            )
        )

        if not retrieved_chunks and not allow_general_answer:
            return LocalFallbackLLMProvider.generate_answer(
                question=question,
                retrieved_chunks=retrieved_chunks,
                prompt=prompt,
                config=config,
                allow_general_answer=allow_general_answer,
                allow_extractive_fallback=allow_extractive_fallback,
            )

        if allow_general_answer:
            system_content = (
                "You are DocuX External AI. Follow the supplied prompt and evidence. "
                "Use document context when present and web context when present. "
                "Do not invent facts or sources. If evidence is insufficient for a "
                "current factual claim, say that it could not be verified."
            )
        else:
            system_content = (
                "You are DocuX AI, a secure private document assistant. "
                "Answer only using the supplied document context. "
                "Do not invent facts. If the context does not contain the answer, say so. "
                "Keep the answer concise and include source names when possible."
            )

        started_at = time.perf_counter()

        try:
            response = requests.post(
                f"{endpoint}/api/chat",
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "think": False,
                    "keep_alive": os.getenv("OLLAMA_REQUEST_KEEP_ALIVE", "30m"),
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                        "num_ctx": num_ctx,
                    },
                },
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()

            answer = data.get("message", {}).get("content", "").strip()

            if not answer:
                answer = "No answer generated by local Ollama model."

            eval_count = int(data.get("eval_count", 0) or 0)
            prompt_eval_count = int(data.get("prompt_eval_count", 0) or 0)
            wall_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

            logger.info(
                "ollama_answer_complete model=%s wall_ms=%s prompt_tokens=%s "
                "completion_tokens=%s num_ctx=%s max_tokens=%s",
                model_name,
                wall_duration_ms,
                prompt_eval_count,
                eval_count,
                num_ctx,
                max_tokens,
            )

            return {
                "answer": answer,
                "provider": cls.provider_name,
                "model": model_name,
                "prompt_tokens": prompt_eval_count,
                "completion_tokens": eval_count,
                "total_tokens": prompt_eval_count + eval_count,
                "metadata": {
                    "api": "ollama_api_chat",
                    "endpoint": endpoint,
                    "done": data.get("done"),
                    "wall_duration_ms": wall_duration_ms,
                    "total_duration": data.get("total_duration"),
                    "load_duration": data.get("load_duration"),
                    "prompt_eval_duration": data.get("prompt_eval_duration"),
                    "eval_duration": data.get("eval_duration"),
                    "num_ctx": num_ctx,
                    "max_tokens": max_tokens,
                    "request_timeout_seconds": timeout,
                },
            }

        except Exception as exc:
            wall_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            error_response = getattr(exc, "response", None)
            error_body = ""
            if error_response is not None and not generation_options.get(
                "suppress_error_details"
            ):
                error_body = (getattr(error_response, "text", "") or "")[:1200]

            logger.warning(
                "ollama_answer_failed model=%s wall_ms=%s num_ctx=%s "
                "max_tokens=%s error=%s response=%s",
                model_name,
                wall_duration_ms,
                num_ctx,
                max_tokens,
                "provider_request_failed"
                if generation_options.get("suppress_error_details")
                else exc,
                error_body,
            )

            result = LocalFallbackLLMProvider.generate_answer(
                question=question,
                retrieved_chunks=retrieved_chunks,
                prompt=prompt,
                config=config,
                allow_general_answer=allow_general_answer,
                allow_extractive_fallback=allow_extractive_fallback,
            )
            result["metadata"]["fallback_reason"] = "ollama_request_failed"
            result["metadata"]["fallback_error"] = str(exc)
            result["metadata"]["fallback_error_type"] = type(exc).__name__
            result["metadata"]["wall_duration_ms"] = wall_duration_ms
            result["metadata"]["ollama_endpoint"] = endpoint
            result["metadata"]["ollama_model"] = model_name
            result["metadata"]["ollama_num_ctx"] = num_ctx
            result["metadata"]["ollama_max_tokens"] = max_tokens
            result["metadata"]["request_timeout_seconds"] = timeout
            if error_body:
                result["metadata"]["ollama_error_response"] = error_body
            return result


class OpenAILLMProvider:
    provider_name = "openai"

    @classmethod
    def generate_answer(
        cls,
        question,
        retrieved_chunks,
        prompt=None,
        config=None,
        allow_general_answer=False,
    ):
        api_key = config.get_api_key() if config else ""

        if not api_key:
            if getattr(config, "_entitlement_enforced", False):
                raise ModelProviderUnavailable("Selected OpenAI credentials are unavailable.")
            result = LocalFallbackLLMProvider.generate_answer(
                question,
                retrieved_chunks,
                prompt,
                config,
            )
            result["metadata"]["fallback_reason"] = "missing_openai_api_key"
            return result

        model_name = config.model_name or "gpt-4.1-mini"

        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_name,
                    "input": prompt,
                    "temperature": float(config.temperature),
                    "max_output_tokens": config.max_tokens,
                },
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()

            answer = cls._extract_response_text(data)
            usage = data.get("usage") or {}

            return {
                "answer": answer,
                "provider": cls.provider_name,
                "model": model_name,
                "prompt_tokens": usage.get("input_tokens", 0) or 0,
                "completion_tokens": usage.get("output_tokens", 0) or 0,
                "total_tokens": usage.get("total_tokens", 0) or 0,
                "metadata": {
                    "response_id": data.get("id"),
                    "api": "responses",
                },
            }

        except Exception as exc:
            if getattr(config, "_entitlement_enforced", False):
                logger.warning("selected_openai_request_failed")
                raise ModelProviderUnavailable() from exc
            result = LocalFallbackLLMProvider.generate_answer(
                question,
                retrieved_chunks,
                prompt,
                config,
            )
            result["metadata"]["fallback_reason"] = "openai_request_failed"
            result["metadata"]["fallback_error"] = str(exc)
            return result

    @staticmethod
    def _extract_response_text(data):
        if data.get("output_text"):
            return data["output_text"]

        output = data.get("output", [])
        text_parts = []

        for item in output:
            for content in item.get("content", []):
                if content.get("type") in ["output_text", "text"]:
                    text_parts.append(content.get("text", ""))

        return (
            "\n".join(part for part in text_parts if part).strip()
            or "No answer generated."
        )


class AzureOpenAILLMProvider:
    provider_name = "azure_openai"

    @classmethod
    def generate_answer(cls, question, retrieved_chunks, prompt=None, config=None):
        api_key = config.get_api_key() if config else ""
        endpoint = (config.endpoint_url or "").rstrip("/") if config else ""
        deployment_name = config.deployment_name if config else ""
        api_version = config.api_version or "2024-10-21"

        if not api_key or not endpoint or not deployment_name:
            result = LocalFallbackLLMProvider.generate_answer(
                question,
                retrieved_chunks,
                prompt,
                config,
            )
            result["metadata"]["fallback_reason"] = "missing_azure_openai_config"
            return result

        try:
            endpoint = LLMProviderSecurityService.validate_endpoint(
                LLMProviderConfig.ProviderType.AZURE_OPENAI,
                endpoint,
            )

            deployment_name = LLMProviderSecurityService.validate_azure_deployment_name(
                deployment_name
            )

            api_version = LLMProviderSecurityService.validate_azure_api_version(
                api_version
            )

        except ValueError as exc:
            logger.warning(
                "azure_openai_endpoint_blocked endpoint=%s error=%s",
                endpoint,
                exc,
            )

            result = LocalFallbackLLMProvider.generate_answer(
                question,
                retrieved_chunks,
                prompt,
                config,
            )

            result["metadata"]["fallback_reason"] = (
                "unsafe_azure_openai_endpoint_blocked"
            )
            result["metadata"]["fallback_error"] = str(exc)

            return result

        url = (
            f"{endpoint}/openai/deployments/{deployment_name}/chat/completions"
            f"?api-version={api_version}"
        )

        try:
            response = requests.post(
                url,
                headers={
                    "api-key": api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are DocuX AI. Answer only from the provided document context and cite sources.",
                        },
                        {
                            "role": "user",
                            "content": prompt,
                        },
                    ],
                    "temperature": float(config.temperature),
                    "max_tokens": config.max_tokens,
                },
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()

            answer = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )

            usage = data.get("usage") or {}

            return {
                "answer": answer or "No answer generated.",
                "provider": cls.provider_name,
                "model": deployment_name,
                "prompt_tokens": usage.get("prompt_tokens", 0) or 0,
                "completion_tokens": usage.get("completion_tokens", 0) or 0,
                "total_tokens": usage.get("total_tokens", 0) or 0,
                "metadata": {
                    "api": "azure_chat_completions",
                    "deployment_name": deployment_name,
                    "api_version": api_version,
                },
            }

        except Exception as exc:
            result = LocalFallbackLLMProvider.generate_answer(
                question,
                retrieved_chunks,
                prompt,
                config,
            )
            result["metadata"]["fallback_reason"] = "azure_openai_request_failed"
            result["metadata"]["fallback_error"] = str(exc)
            return result


class LLMGatewayService:
    @classmethod
    def generate_answer(
        cls,
        question,
        retrieved_chunks,
        prompt=None,
        provider_config=None,
        allow_general_answer=False,
        generation_options=None,
        allow_extractive_fallback=True,
    ):
        if not provider_config:
            return LocalFallbackLLMProvider.generate_answer(
                question=question,
                retrieved_chunks=retrieved_chunks,
                prompt=prompt,
                allow_general_answer=allow_general_answer,
                allow_extractive_fallback=allow_extractive_fallback,
            )

        # An entitlement-selected Ollama model may not be overridden by caller options.
        if getattr(provider_config, "_entitlement_enforced", False):
            generation_options = {k: v for k, v in (generation_options or {}).items() if k != "model"}

        if provider_config.provider_type == LLMProviderConfig.ProviderType.OLLAMA:
            return OllamaLLMProvider.generate_answer(
                question=question,
                retrieved_chunks=retrieved_chunks,
                prompt=prompt,
                config=provider_config,
                allow_general_answer=allow_general_answer,
                generation_options=generation_options,
                allow_extractive_fallback=allow_extractive_fallback,
            )

        if provider_config.provider_type == LLMProviderConfig.ProviderType.GROQ:
            return GroqLLMProvider.generate_answer(question, retrieved_chunks, prompt, provider_config)
        if provider_config.provider_type == LLMProviderConfig.ProviderType.GEMINI:
            return GeminiLLMProvider.generate_answer(question, retrieved_chunks, prompt, provider_config)
        if provider_config.provider_type == LLMProviderConfig.ProviderType.ANTHROPIC:
            return AnthropicLLMProvider.generate_answer(question, retrieved_chunks, prompt, provider_config)

        if provider_config.provider_type == LLMProviderConfig.ProviderType.OPENAI:
            return OpenAILLMProvider.generate_answer(
                question=question,
                retrieved_chunks=retrieved_chunks,
                prompt=prompt,
                config=provider_config,
            )

        if provider_config.provider_type == LLMProviderConfig.ProviderType.AZURE_OPENAI:
            return AzureOpenAILLMProvider.generate_answer(
                question=question,
                retrieved_chunks=retrieved_chunks,
                prompt=prompt,
                config=provider_config,
            )

        return LocalFallbackLLMProvider.generate_answer(
            question=question,
            retrieved_chunks=retrieved_chunks,
            prompt=prompt,
            config=provider_config,
            allow_general_answer=allow_general_answer,
            allow_extractive_fallback=allow_extractive_fallback,
        )
