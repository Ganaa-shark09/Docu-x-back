import re

import requests

from apps.ai_engine.models import LLMProviderConfig


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
    def generate_answer(cls, question, retrieved_chunks, prompt=None, config=None):
        if not retrieved_chunks:
            return {
                "answer": "I could not find relevant information in the indexed company documents.",
                "provider": cls.provider_name,
                "model": cls.model_name,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "metadata": {"fallback_reason": "no_retrieved_chunks"},
            }

        best_sentences = cls._find_relevant_sentences(question, retrieved_chunks)

        if not best_sentences:
            top_chunk = retrieved_chunks[0]
            answer = (
                "The uploaded documents contain related information, but I could not "
                "extract a precise answer. The most relevant source says:\n\n"
                f"{top_chunk.get('content', '')[:700]}"
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
    def generate_answer(cls, question, retrieved_chunks, prompt=None, config=None):
        endpoint = (
            (config.endpoint_url or "http://ollama:11434").rstrip("/")
            if config
            else "http://ollama:11434"
        )
        model_name = config.model_name or "llama3.2:1b"
        temperature = float(config.temperature) if config else 0.2
        max_tokens = config.max_tokens if config else 1200

        if not retrieved_chunks:
            return LocalFallbackLLMProvider.generate_answer(
                question=question,
                retrieved_chunks=retrieved_chunks,
                prompt=prompt,
                config=config,
            )

        try:
            response = requests.post(
                f"{endpoint}/api/chat",
                json={
                    "model": model_name,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are DocuX AI, a secure private document assistant. "
                                "Answer only using the supplied document context. "
                                "Do not invent facts. If the context does not contain the answer, say so. "
                                "Keep the answer concise and include source names when possible."
                            ),
                        },
                        {
                            "role": "user",
                            "content": prompt,
                        },
                    ],
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                },
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()

            answer = data.get("message", {}).get("content", "").strip()

            if not answer:
                answer = "No answer generated by local Ollama model."

            eval_count = data.get("eval_count", 0) or 0
            prompt_eval_count = data.get("prompt_eval_count", 0) or 0

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
                    "total_duration": data.get("total_duration"),
                    "load_duration": data.get("load_duration"),
                },
            }

        except Exception as exc:
            result = LocalFallbackLLMProvider.generate_answer(
                question=question,
                retrieved_chunks=retrieved_chunks,
                prompt=prompt,
                config=config,
            )
            result["metadata"]["fallback_reason"] = "ollama_request_failed"
            result["metadata"]["fallback_error"] = str(exc)
            result["metadata"]["ollama_endpoint"] = endpoint
            result["metadata"]["ollama_model"] = model_name
            return result


class OpenAILLMProvider:
    provider_name = "openai"

    @classmethod
    def generate_answer(cls, question, retrieved_chunks, prompt=None, config=None):
        api_key = config.get_api_key() if config else ""

        if not api_key:
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
    ):
        if not provider_config:
            return LocalFallbackLLMProvider.generate_answer(
                question=question,
                retrieved_chunks=retrieved_chunks,
                prompt=prompt,
            )

        if provider_config.provider_type == LLMProviderConfig.ProviderType.OLLAMA:
            return OllamaLLMProvider.generate_answer(
                question=question,
                retrieved_chunks=retrieved_chunks,
                prompt=prompt,
                config=provider_config,
            )

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
        )
