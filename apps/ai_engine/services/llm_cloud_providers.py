"""Fixed-endpoint cloud AI transports. Credential and endpoint values never originate in client requests."""
import logging
import requests
from rest_framework.exceptions import APIException

logger = logging.getLogger(__name__)


class ModelProviderUnavailable(APIException):
    status_code = 502
    default_detail = "The selected AI model is unavailable. Please try again or select another approved model."
    default_code = "model_provider_unavailable"


def _request(url, headers, payload):
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("model_provider_request_failed")
        raise ModelProviderUnavailable() from exc


def _credential(config):
    key = config.get_api_key() if config else ""
    if not key:
        raise ModelProviderUnavailable("The selected provider credentials are unavailable.")
    return key


class GroqLLMProvider:
    provider_name = "groq"

    @classmethod
    def generate_answer(cls, question, retrieved_chunks, prompt=None, config=None):
        key = _credential(config)
        model = config.model_name
        data = _request(
            "https://api.groq.com/openai/v1/chat/completions",
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            {"model": model, "messages": [{"role": "user", "content": prompt or question}],
             "max_tokens": config.max_tokens, "temperature": float(config.temperature)},
        )
        try:
            answer = data["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError) as exc:
            raise ModelProviderUnavailable("The selected provider returned an invalid response.") from exc
        usage = data.get("usage") or {}
        return {"answer": answer or "No answer generated.", "provider": cls.provider_name, "model": model,
                "prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0), "metadata": {"api": "groq_chat_completions"}}


class GeminiLLMProvider:
    provider_name = "gemini"

    @classmethod
    def generate_answer(cls, question, retrieved_chunks, prompt=None, config=None):
        key = _credential(config)
        model = config.model_name
        data = _request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            {"x-goog-api-key": key, "Content-Type": "application/json"},
            {"contents": [{"role": "user", "parts": [{"text": prompt or question}]}],
             "generationConfig": {"maxOutputTokens": config.max_tokens, "temperature": float(config.temperature)}},
        )
        answer = "\n".join(part.get("text", "") for candidate in data.get("candidates", [])
                           for part in (candidate.get("content") or {}).get("parts", []) if part.get("text"))
        usage = data.get("usageMetadata") or {}
        return {"answer": answer or "No answer generated.", "provider": cls.provider_name, "model": model,
                "prompt_tokens": usage.get("promptTokenCount", 0), "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0), "metadata": {"api": "gemini_generate_content"}}


class AnthropicLLMProvider:
    provider_name = "anthropic"

    @classmethod
    def generate_answer(cls, question, retrieved_chunks, prompt=None, config=None):
        key = _credential(config)
        model = config.model_name
        data = _request(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            {"model": model, "max_tokens": config.max_tokens,
             "messages": [{"role": "user", "content": prompt or question}]},
        )
        answer = "\n".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
        usage = data.get("usage") or {}
        prompt_tokens, completion_tokens = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        return {"answer": answer or "No answer generated.", "provider": cls.provider_name, "model": model,
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens, "metadata": {"api": "anthropic_messages"}}
