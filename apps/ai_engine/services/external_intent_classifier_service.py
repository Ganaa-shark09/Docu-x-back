import json
import logging
import os
import time

import requests

from apps.ai_engine.services.calculator_tool_service import CalculatorToolService

logger = logging.getLogger(__name__)


class ExternalIntentClassifierService:
    """Legacy standalone classifier.

    The live External AI request path currently uses
    ExternalSemanticOrchestratorService, which combines semantic routing with a
    direct answer to avoid a second model call for normal conversation. This
    classifier is kept for compatibility with existing imports and targeted
    tests, but it no longer relies on phrase-specific fallback rules.
    """

    VALID_INTENTS = {
        "smalltalk",
        "assistant_identity",
        "calculator",
        "document_question",
        "web_question",
        "general_question",
        "mixed_document_web",
    }

    @classmethod
    def classify(
        cls,
        message,
        has_attached_documents=False,
        explicit_document_uuids=None,
        use_web=True,
    ):
        calculation = CalculatorToolService.calculate(message)

        if calculation:
            return {
                "intent": "calculator",
                "calculation": calculation,
                "should_use_documents": False,
                "should_use_web": False,
                "should_use_llm": False,
                "confidence": 1.0,
                "reason": "Detected deterministic arithmetic expression.",
            }

        if explicit_document_uuids:
            return {
                "intent": "document_question",
                "calculation": None,
                "should_use_documents": True,
                "should_use_web": False,
                "should_use_llm": True,
                "confidence": 1.0,
                "reason": "User explicitly selected external documents.",
            }

        llm_result = cls.classify_with_ollama(
            message=message,
            has_attached_documents=has_attached_documents,
            use_web=use_web,
        )

        if llm_result:
            return cls.normalize_route(
                llm_result,
                has_attached_documents=has_attached_documents,
                use_web=use_web,
            )

        return cls.fallback_classify(
            has_attached_documents=has_attached_documents,
            use_web=use_web,
        )

    @classmethod
    def classify_with_ollama(cls, message, has_attached_documents=False, use_web=True):
        model = os.getenv(
            "OLLAMA_INTENT_MODEL",
            os.getenv("OLLAMA_DEFAULT_MODEL", "qwen3:8b"),
        )

        base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
        timeout = int(os.getenv("OLLAMA_INTENT_TIMEOUT_SECONDS", "90"))
        started_at = time.perf_counter()

        prompt = cls.build_prompt(
            message=message,
            has_attached_documents=has_attached_documents,
            use_web=use_web,
        )

        try:
            response = requests.post(
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "think": False,
                    "keep_alive": os.getenv("OLLAMA_REQUEST_KEEP_ALIVE", "30m"),
                    "format": "json",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are an intent classifier for DocuX External AI. "
                                "Return only valid JSON. Do not answer the user."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "options": {
                        "temperature": 0,
                        "num_predict": int(
                            os.getenv("OLLAMA_INTENT_MAX_TOKENS", "120")
                        ),
                        "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "4096")),
                    },
                },
                timeout=timeout,
            )
            response.raise_for_status()

            data = response.json()
            content = data.get("message", {}).get("content", "").strip()

            if not content:
                return None

            result = json.loads(content)
            logger.info(
                "external_ai_intent_complete model=%s wall_ms=%s",
                model,
                round((time.perf_counter() - started_at) * 1000, 2),
            )
            return result

        except Exception as exc:
            logger.warning(
                "external_ai_intent_failed model=%s wall_ms=%s error=%s",
                model,
                round((time.perf_counter() - started_at) * 1000, 2),
                exc,
            )
            return None

    @staticmethod
    def build_prompt(message, has_attached_documents=False, use_web=True):
        return f"""
Classify this user message for an external AI assistant.

The assistant can:
- answer normal conversation and self-introduction questions
- answer stable general questions
- use web search for public/current facts
- use uploaded external documents/images when the question asks about them

Important:
- Greetings, assistant identity, and capability questions do not need web search.
- Use document_question only when the request actually depends on an attached file.
- Use web_question for current/changing public information that should be verified externally.
- Use mixed_document_web only when both attached content and external information are needed.
- Use general_question for stable knowledge or open-ended questions that need neither documents nor web.
- Attached documents may exist without being relevant to the current message.

Context:
has_attached_documents: {has_attached_documents}
web_search_enabled: {use_web}

Return JSON exactly in this shape:
{{
  "intent": "smalltalk | assistant_identity | document_question | web_question | general_question | mixed_document_web",
  "should_use_documents": true/false,
  "should_use_web": true/false,
  "should_use_llm": true/false,
  "confidence": 0.0 to 1.0,
  "reason": "short reason"
}}

User message:
{message}
""".strip()

    @classmethod
    def normalize_route(cls, result, has_attached_documents=False, use_web=True):
        intent = result.get("intent", "general_question")

        if intent not in cls.VALID_INTENTS:
            intent = "general_question"

        if intent == "assistant_identity":
            intent = "smalltalk"

        if intent == "smalltalk":
            should_use_documents = False
            should_use_web = False
            should_use_llm = True
        elif intent == "document_question":
            should_use_documents = bool(has_attached_documents)
            should_use_web = False
            should_use_llm = True
        elif intent == "mixed_document_web":
            should_use_documents = bool(has_attached_documents)
            should_use_web = bool(use_web)
            should_use_llm = True
        elif intent == "web_question":
            should_use_documents = False
            should_use_web = bool(use_web)
            should_use_llm = True
        else:
            should_use_documents = False
            should_use_web = False
            should_use_llm = True

        return {
            "intent": intent,
            "calculation": None,
            "should_use_documents": should_use_documents,
            "should_use_web": should_use_web,
            "should_use_llm": should_use_llm,
            "confidence": float(result.get("confidence", 0.7) or 0.7),
            "reason": result.get("reason", ""),
        }

    @staticmethod
    def fallback_classify(has_attached_documents=False, use_web=True):
        # Fail safely without phrase-specific hardcoding. The live external chat
        # path uses the semantic orchestrator, so this is only a compatibility
        # fallback if the standalone classifier cannot reach Ollama.
        return {
            "intent": "general_question",
            "calculation": None,
            "should_use_documents": False,
            "should_use_web": False,
            "should_use_llm": True,
            "confidence": 0.0,
            "reason": "Intent model unavailable; defaulted to general conversation.",
        }
