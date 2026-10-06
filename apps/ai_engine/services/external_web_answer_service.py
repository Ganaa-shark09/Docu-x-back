import os

from apps.ai_engine.services.external_ai_model_policy import (
    ExternalAIModelPolicy,
)

from apps.ai_engine.services.llm_gateway_service import (
    LLMGatewayService,
)


class ExternalWebAnswerService:
    """
    Final synthesis stage for public web evidence.

    Responsibilities:
      - keep web evidence bounded
      - use the web-specific answer model
      - retry with the configured fast fallback model when applicable
      - return useful evidence-backed output even if model synthesis fails

    This service contains no person-, publisher-, country-, article-,
    political-, or query-specific production rules.
    """

    @staticmethod
    def should_handle(
        web_results,
        document_chunks,
    ):
        """
        Use dedicated web synthesis only for a pure-web turn.

        document + web turns continue through the combined
        document/web answer path.
        """
        return bool(web_results) and not bool(document_chunks)

    @classmethod
    def generate(
        cls,
        question,
        web_results,
        provider_config,
    ):
        evidence = cls.compact_results(
            question=question,
            web_results=web_results,
        )

        if not evidence:
            return cls._evidence_fallback(
                question=question,
                evidence=[],
                failure_metadata={
                    "fallback_reason": "no_compact_web_evidence",
                },
            )

        prompt = cls.build_prompt(
            question=question,
            evidence=evidence,
        )

        gateway_chunks = cls._gateway_chunks(evidence)

        primary_model = ExternalAIModelPolicy.web_answer_model()

        generation_options = {
            "model": primary_model,
            "timeout": int(
                os.getenv(
                    "OLLAMA_WEB_ANSWER_TIMEOUT_SECONDS",
                    "90",
                )
            ),
            "max_tokens": int(
                os.getenv(
                    "OLLAMA_WEB_ANSWER_MAX_TOKENS",
                    "450",
                )
            ),
            "num_ctx": int(
                os.getenv(
                    "OLLAMA_WEB_ANSWER_NUM_CTX",
                    "4096",
                )
            ),
        }

        primary = LLMGatewayService.generate_answer(
            question=question,
            retrieved_chunks=gateway_chunks,
            prompt=prompt,
            provider_config=provider_config,
            allow_general_answer=True,
            generation_options=(generation_options),
            allow_extractive_fallback=True,
        )

        if not cls._generation_failed(primary):
            metadata = dict(primary.get("metadata") or {})

            metadata["web_answer_synthesis"] = {
                "evidence_count": len(evidence),
                "primary_model": primary_model,
                "fallback_used": False,
            }

            primary["metadata"] = metadata

            return primary

        fallback_model = ExternalAIModelPolicy.web_answer_fallback_model()

        primary_metadata = dict(primary.get("metadata") or {})

        #
        # Retry only when the failed provider was actually Ollama
        # and a different fallback model is configured.
        #
        if (
            fallback_model
            and fallback_model != primary_model
            and primary_metadata.get("ollama_model")
        ):
            retry_options = dict(generation_options)

            retry_options["model"] = fallback_model

            retry_options["timeout"] = int(
                os.getenv(
                    ("OLLAMA_WEB_ANSWER_" "FALLBACK_TIMEOUT_SECONDS"),
                    "75",
                )
            )

            retry = LLMGatewayService.generate_answer(
                question=question,
                retrieved_chunks=(gateway_chunks),
                prompt=prompt,
                provider_config=(provider_config),
                allow_general_answer=True,
                generation_options=(retry_options),
                allow_extractive_fallback=True,
            )

            if not cls._generation_failed(retry):
                metadata = dict(retry.get("metadata") or {})

                metadata["web_answer_synthesis"] = {
                    "evidence_count": len(evidence),
                    "primary_model": primary_model,
                    "fallback_model": fallback_model,
                    "fallback_used": True,
                }

                retry["metadata"] = metadata

                return retry

            failure_metadata = {
                "primary": primary_metadata,
                "retry": retry.get("metadata") or {},
            }

        else:
            failure_metadata = {
                "primary": primary_metadata,
            }

        #
        # Last-resort behaviour:
        #
        # Do not throw away already-retrieved current web evidence
        # merely because a local model is temporarily overloaded.
        #
        return cls._evidence_fallback(
            question=question,
            evidence=evidence,
            failure_metadata=(failure_metadata),
        )

    @classmethod
    def compact_results(
        cls,
        question,
        web_results,
    ):
        max_results = max(
            1,
            min(
                8,
                int(
                    os.getenv(
                        ("EXTERNAL_WEB_ANSWER_" "MAX_RESULTS"),
                        "4",
                    )
                ),
            ),
        )

        per_source_limit = max(
            500,
            int(
                os.getenv(
                    ("EXTERNAL_WEB_ANSWER_" "PER_SOURCE_CHARS"),
                    "1600",
                )
            ),
        )

        total_limit = max(
            per_source_limit,
            int(
                os.getenv(
                    ("EXTERNAL_WEB_ANSWER_" "TOTAL_CHARS"),
                    "6400",
                )
            ),
        )

        output = []
        used_chars = 0

        for raw in web_results or []:
            if len(output) >= max_results:
                break

            item = dict(raw or {})

            title = cls._normalize_text(item.get("title"))

            url = str(item.get("url") or "").strip()

            snippet = cls._normalize_text(item.get("snippet"))

            content = cls._normalize_text(item.get("content"))

            evidence_text = content or snippet

            if not title or not evidence_text:
                continue

            remaining = total_limit - used_chars

            if remaining <= 0:
                break

            allowed = min(
                per_source_limit,
                remaining,
            )

            excerpt = cls._excerpt(
                evidence_text,
                limit=allowed,
            )

            if not excerpt:
                continue

            output.append(
                {
                    "title": title,
                    "url": url,
                    "published_at": item.get("published_at"),
                    "source": item.get("source"),
                    "content_source": item.get("content_source")
                    or ("publisher_page" if content else ("search_result_" "snippet")),
                    "snippet": snippet[:600],
                    "evidence": excerpt,
                }
            )

            used_chars += len(excerpt)

        return output

    @classmethod
    def build_prompt(
        cls,
        question,
        evidence,
    ):
        parts = [
            (
                "You are DocuX External AI. "
                "Answer the user's public/external "
                "question using the supplied current "
                "web evidence."
            ),
            (
                "Rules:\n"
                "1. Answer the user's question directly.\n"
                "2. Use only the supplied web evidence for current facts.\n"
                "3. Do not replace current evidence with model memory.\n"
                "4. Do not invent headlines, dates, names, quotes, or sources.\n"
                "5. For news requests, summarize the most relevant current items clearly.\n"
                "6. If several results repeat the same story, combine them rather than repeating them.\n"
                "7. If evidence conflicts, state that clearly.\n"
                "8. Search-result snippets are weaker than fetched publisher-page evidence.\n"
                "9. Do not claim to have read content that is not present below.\n"
                "10. Keep the response useful and concise."
            ),
            ("USER QUESTION:\n" f"{question}"),
            "WEB EVIDENCE:",
        ]

        for index, item in enumerate(
            evidence,
            start=1,
        ):
            parts.append(
                (
                    f"[Web Source {index}]\n"
                    f"Title: "
                    f"{item.get('title')}\n"
                    f"Published: "
                    f"{item.get('published_at') or 'unknown'}\n"
                    f"URL: "
                    f"{item.get('url') or 'unknown'}\n"
                    f"Evidence type: "
                    f"{item.get('content_source') or 'unknown'}\n"
                    f"Evidence:\n"
                    f"{item.get('evidence') or ''}"
                )
            )

        parts.append(
            (
                "Now produce the final answer. "
                "Do not discuss the search pipeline "
                "unless the user asks."
            )
        )

        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _gateway_chunks(
        evidence,
    ):
        return [
            {
                "content": item.get("evidence") or "",
                "document_title": item.get("title") or (f"Web Source " f"{index}"),
                "chunk_index": index,
            }
            for index, item in enumerate(
                evidence,
                start=1,
            )
        ]

    @staticmethod
    def _generation_failed(
        result,
    ):
        if not result:
            return True

        provider = str(result.get("provider") or "").strip().lower()

        if provider == "local_fallback":
            return True

        answer = str(result.get("answer") or "").strip()

        return not bool(answer)

    @classmethod
    def _evidence_fallback(
        cls,
        question,
        evidence,
        failure_metadata,
    ):
        if not evidence:
            answer = (
                "I couldn't complete the web "
                "answer because no usable public "
                "evidence was available."
            )

        else:
            lines = [
                (
                    "I found current web evidence, "
                    "but the language-model synthesis "
                    "did not complete. Here are the "
                    "most relevant results:"
                )
            ]

            for item in evidence:
                summary = item.get("snippet") or item.get("evidence") or ""

                summary = cls._excerpt(
                    summary,
                    limit=320,
                )

                lines.append((f"- {item.get('title')}: " f"{summary}"))

            answer = "\n\n".join(lines)

        return {
            "answer": answer,
            "provider": "web_evidence_fallback",
            "model": "deterministic_web_evidence",
            "prompt_tokens": 0,
            "completion_tokens": len(answer.split()),
            "total_tokens": len(answer.split()),
            "metadata": {
                "fallback_reason": ("web_llm_generation_" "failed_evidence_returned"),
                "failure_metadata": failure_metadata,
                "question": question,
                "evidence_count": len(evidence),
            },
        }

    @staticmethod
    def _normalize_text(
        value,
    ):
        return " ".join(str(value or "").split())

    @staticmethod
    def _excerpt(
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
