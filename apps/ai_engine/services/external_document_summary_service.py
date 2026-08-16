import hashlib
import logging
import math
import os
import re

from apps.ai_engine.services.external_ai_model_policy import ExternalAIModelPolicy
from apps.ai_engine.services.external_document_retrieval_service import (
    ExternalDocumentRetrievalService,
)
from apps.ai_engine.services.llm_gateway_service import LLMGatewayService
from apps.ai_engine.services.llm_provider_config_service import LLMProviderConfigService
from apps.documents.models import Document

logger = logging.getLogger(__name__)


class ExternalDocumentSummaryService:
    """Coverage-first whole-document summarization.

    Whole-file summaries must not use relevance-ranked top-k retrieval because
    low-scoring pages/sections can be silently omitted. The service therefore
    reads every ready target document in source order and chooses between:

    * token-aware single-pass summarization for documents that safely fit the
      summary context window; and
    * hierarchical map/reduce summarization for larger documents.

    Hierarchical map failures are recovered by splitting the failing part into
    smaller sub-parts instead of immediately failing the whole summary.
    """

    @classmethod
    def summarize(
        cls,
        user,
        conversation,
        question,
        explicit_document_uuids=None,
    ):
        documents = ExternalDocumentRetrievalService.get_candidate_documents(
            user=user,
            conversation=conversation,
            explicit_document_uuids=explicit_document_uuids,
        )

        ready_documents = [
            document
            for document in documents
            if document.is_ai_ready and document.status != Document.Status.FAILED
        ]

        if not ready_documents:
            return None

        provider_config = LLMProviderConfigService.get_default_config_for_company(
            user.company
        )

        # Single-pass is reserved for genuinely small sources. Context-window
        # capacity alone is not a performance budget on CPU: a 6k-token prompt
        # can fit in 8k context and still take many minutes. Medium/large files
        # therefore go directly to fast-model coverage + quality-model synthesis.
        if len(ready_documents) == 1:
            document = ready_documents[0]
            full_text = cls.get_extracted_text(document)

            if full_text and cls.should_use_single_pass(full_text, question):
                result = cls.summarize_single_pass(
                    document=document,
                    full_text=full_text,
                    question=question,
                    provider_config=provider_config,
                )

                if result.get("provider") != "system":
                    return result

                # If the complete pass times out or otherwise fails, degrade to
                # coverage-preserving hierarchical summarization rather than
                # returning an immediate failure to the user.
                logger.warning(
                    "external_document_summary_single_pass_degraded "
                    "document=%s chars=%s estimated_tokens=%s metadata=%s",
                    document.uuid,
                    len(full_text),
                    cls.estimate_tokens(full_text),
                    result.get("metadata", {}),
                )

        return cls.summarize_hierarchical(
            ready_documents=ready_documents,
            question=question,
            provider_config=provider_config,
        )

    @classmethod
    def summarize_hierarchical(cls, ready_documents, question, provider_config):
        document_notes = []
        sources = []
        total_prompt_tokens = 0
        total_completion_tokens = 0
        coverage = []
        provider = ""
        model = ""

        for document in ready_documents:
            full_text = cls.get_extracted_text(document)
            if not full_text:
                continue

            chunks = cls.split_for_summary(full_text)
            if not chunks:
                continue

            cached_coverage = cls.load_coverage_cache(document, full_text)
            chunk_notes = []
            effective_parts = 0
            coverage_cache_hit = False

            if cached_coverage:
                chunk_notes = list(cached_coverage["notes"])
                effective_parts = int(
                    cached_coverage.get("effective_parts", len(chunk_notes))
                )
                coverage_cache_hit = True
                model = ExternalAIModelPolicy.summary_map_model()
                logger.info(
                    "external_document_summary_coverage_cache_hit "
                    "document=%s parts=%s effective_parts=%s model=%s",
                    document.uuid,
                    len(chunks),
                    effective_parts,
                    model,
                )
            else:
                logger.info(
                    "external_document_summary_strategy document=%s strategy=hierarchical "
                    "chars=%s estimated_tokens=%s initial_parts=%s map_model=%s final_model=%s",
                    document.uuid,
                    len(full_text),
                    cls.estimate_tokens(full_text),
                    len(chunks),
                    ExternalAIModelPolicy.summary_map_model(),
                    ExternalAIModelPolicy.summary_final_model(),
                )

                for index, chunk in enumerate(chunks, start=1):
                    map_outcome = cls.summarize_map_part_with_recovery(
                        document=document,
                        chunk=chunk,
                        question=question,
                        provider_config=provider_config,
                        label=f"{index}/{len(chunks)}",
                        depth=0,
                    )

                    if not map_outcome["ok"]:
                        logger.warning(
                            "external_document_summary_map_unrecoverable "
                            "document=%s part=%s/%s metadata=%s",
                            document.uuid,
                            index,
                            len(chunks),
                            map_outcome.get("metadata", {}),
                        )
                        return cls.failure_result(
                            document=document,
                            stage="map",
                            metadata={
                                **map_outcome.get("metadata", {}),
                                "failed_part": index,
                                "initial_parts": len(chunks),
                            },
                        )

                    notes = map_outcome.get("notes", [])
                    chunk_notes.extend(notes)
                    effective_parts += len(notes)
                    total_prompt_tokens += int(map_outcome.get("prompt_tokens", 0) or 0)
                    total_completion_tokens += int(
                        map_outcome.get("completion_tokens", 0) or 0
                    )
                    provider = map_outcome.get("provider", provider)
                    model = map_outcome.get("model", model)

                if chunk_notes:
                    cls.save_coverage_cache(
                        document=document,
                        full_text=full_text,
                        notes=chunk_notes,
                        initial_parts=len(chunks),
                        effective_parts=effective_parts,
                    )

            if not chunk_notes:
                continue

            document_notes.append(
                f"DOCUMENT: {document.title}\n"
                f"Initial source parts: {len(chunks)}\n"
                f"Effective coverage parts: {effective_parts}\n\n"
                + "\n\n".join(chunk_notes)
            )
            coverage.append(
                {
                    "document_uuid": str(document.uuid),
                    "document_title": document.title,
                    "character_count": len(full_text),
                    "estimated_source_tokens": cls.estimate_tokens(full_text),
                    "parts": len(chunks),
                    "effective_parts": effective_parts,
                    "all_parts_processed": True,
                    "coverage_cache_hit": coverage_cache_hit,
                    "coverage_model": ExternalAIModelPolicy.summary_map_model(),
                }
            )
            sources.append(cls.build_source(document, full_text))

        if not document_notes:
            return None

        final_prompt = cls.build_final_prompt(
            question=question,
            document_notes=document_notes,
        )
        final_result = cls.generate_final_summary(
            question=question,
            prompt=final_prompt,
            coverage=coverage,
            provider_config=provider_config,
        )

        if final_result.get(
            "provider"
        ) == "local_fallback" and cls.is_retryable_map_failure(
            final_result.get("metadata", {})
        ):
            # A single controlled retry is worthwhile for transient timeout or
            # context-size failures because the model is already warm and the
            # final synthesis prompt is much smaller than the original source.
            logger.warning(
                "external_document_summary_final_retry metadata=%s",
                final_result.get("metadata", {}),
            )
            final_result = cls.generate_final_summary(
                question=question,
                prompt=final_prompt,
                coverage=coverage,
                provider_config=provider_config,
                retry=True,
            )

        if final_result.get("provider") == "local_fallback":
            return cls.failure_result(
                document=ready_documents[0],
                stage="final",
                metadata=final_result.get("metadata", {}),
            )

        total_prompt_tokens += int(final_result.get("prompt_tokens", 0) or 0)
        total_completion_tokens += int(final_result.get("completion_tokens", 0) or 0)

        return {
            "answer": final_result["answer"],
            "sources": sources,
            "provider": final_result.get("provider", provider),
            "model": final_result.get("model", model),
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
            "metadata": {
                "whole_document_summary": True,
                "hierarchical_summary": True,
                "complete": True,
                "documents_processed": len(coverage),
                "coverage": coverage,
                "final_generation_metadata": final_result.get("metadata", {}),
            },
        }

    @classmethod
    def summarize_single_pass(cls, document, full_text, question, provider_config):
        summary_num_ctx = cls.summary_num_ctx()
        summary_max_tokens = cls.summary_max_tokens()

        logger.info(
            "external_document_summary_strategy document=%s strategy=single_pass "
            "chars=%s estimated_tokens=%s num_ctx=%s max_tokens=%s",
            document.uuid,
            len(full_text),
            cls.estimate_tokens(full_text),
            summary_num_ctx,
            summary_max_tokens,
        )

        prompt = (
            "You are DocuX External AI summarizing one complete document.\n\n"
            "Rules:\n"
            "1. Read the entire supplied document before answering.\n"
            "2. Give a clear executive overview first.\n"
            "3. Then explain every material section and important point in a logical structure.\n"
            "4. Preserve important names, dates, numbers, identifiers, requirements, exceptions, risks, statuses, findings, and conclusions.\n"
            "5. When the source contains tables or lists, explain their meaning and important values/trends; do not pretend rows were reviewed if they are absent from the supplied content.\n"
            "6. Do not skip material points merely to make the answer shorter.\n"
            "7. Avoid repetition, but prefer completeness when the source contains substantial information.\n"
            "8. If the user asks for detail or to explain everything, provide a detailed section-by-section explanation.\n"
            "9. Clearly distinguish source facts from your interpretation.\n"
            "10. Do not invent facts that are not present in the document.\n\n"
            f"USER REQUEST:\n{question}\n\n"
            f"DOCUMENT: {document.title}\n\n"
            f"FULL DOCUMENT CONTENT:\n{full_text}\n\n"
            "Final answer:"
        )

        result = LLMGatewayService.generate_answer(
            question=question,
            retrieved_chunks=[
                {
                    "document_title": document.title,
                    "content": full_text,
                    "chunk_index": 0,
                }
            ],
            prompt=prompt,
            provider_config=provider_config,
            allow_general_answer=True,
            generation_options={
                "model": ExternalAIModelPolicy.summary_final_model(),
                "max_tokens": summary_max_tokens,
                "timeout": cls.summary_timeout(),
                "num_ctx": summary_num_ctx,
            },
            allow_extractive_fallback=False,
        )

        if result.get("provider") == "local_fallback":
            return cls.failure_result(
                document=document,
                stage="single_pass",
                metadata=result.get("metadata", {}),
            )

        return {
            "answer": result["answer"],
            "sources": [cls.build_source(document, full_text)],
            "provider": result.get("provider", ""),
            "model": result.get("model", ""),
            "prompt_tokens": result.get("prompt_tokens", 0),
            "completion_tokens": result.get("completion_tokens", 0),
            "total_tokens": result.get("total_tokens", 0),
            "metadata": {
                "whole_document_summary": True,
                "hierarchical_summary": False,
                "complete": True,
                "documents_processed": 1,
                "coverage": [
                    {
                        "document_uuid": str(document.uuid),
                        "document_title": document.title,
                        "character_count": len(full_text),
                        "estimated_source_tokens": cls.estimate_tokens(full_text),
                        "parts": 1,
                        "effective_parts": 1,
                        "all_parts_processed": True,
                    }
                ],
                "final_generation_metadata": result.get("metadata", {}),
            },
        }

    @classmethod
    def summarize_map_part_with_recovery(
        cls,
        document,
        chunk,
        question,
        provider_config,
        label,
        depth,
    ):
        result = cls.generate_map_part(
            document=document,
            chunk=chunk,
            question=question,
            provider_config=provider_config,
            label=label,
        )

        if result.get("provider") != "local_fallback":
            return {
                "ok": True,
                "notes": [f"[Coverage part {label}]\n{result['answer'].strip()}"],
                "provider": result.get("provider", ""),
                "model": result.get("model", ""),
                "prompt_tokens": int(result.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(result.get("completion_tokens", 0) or 0),
                "metadata": result.get("metadata", {}),
            }

        metadata = result.get("metadata", {})
        if not cls.is_retryable_map_failure(metadata):
            return {
                "ok": False,
                "notes": [],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "metadata": {
                    **metadata,
                    "retry_depth": depth,
                    "chunk_characters": len(chunk),
                    "retryable": False,
                },
            }

        max_depth = int(os.getenv("EXTERNAL_AI_SUMMARY_RETRY_MAX_DEPTH", "2"))
        min_chars = int(os.getenv("EXTERNAL_AI_SUMMARY_RETRY_MIN_CHARS", "2200"))

        if depth >= max_depth or len(chunk) <= min_chars:
            return {
                "ok": False,
                "notes": [],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "metadata": {
                    **result.get("metadata", {}),
                    "retry_depth": depth,
                    "chunk_characters": len(chunk),
                },
            }

        subchunks = cls.split_text_for_retry(chunk)
        if len(subchunks) <= 1:
            return {
                "ok": False,
                "notes": [],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "metadata": {
                    **result.get("metadata", {}),
                    "retry_depth": depth,
                    "chunk_characters": len(chunk),
                    "retry_split_failed": True,
                },
            }

        logger.warning(
            "external_document_summary_map_retry_split document=%s label=%s "
            "depth=%s chars=%s subparts=%s metadata=%s",
            document.uuid,
            label,
            depth,
            len(chunk),
            len(subchunks),
            result.get("metadata", {}),
        )

        combined = {
            "ok": True,
            "notes": [],
            "provider": "",
            "model": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "metadata": {"recovered_by_split": True, "retry_depth": depth + 1},
        }

        for sub_index, subchunk in enumerate(subchunks, start=1):
            outcome = cls.summarize_map_part_with_recovery(
                document=document,
                chunk=subchunk,
                question=question,
                provider_config=provider_config,
                label=f"{label}.{sub_index}/{len(subchunks)}",
                depth=depth + 1,
            )
            if not outcome["ok"]:
                return outcome

            combined["notes"].extend(outcome.get("notes", []))
            combined["prompt_tokens"] += int(outcome.get("prompt_tokens", 0) or 0)
            combined["completion_tokens"] += int(
                outcome.get("completion_tokens", 0) or 0
            )
            combined["provider"] = outcome.get("provider", combined["provider"])
            combined["model"] = outcome.get("model", combined["model"])

        return combined

    @staticmethod
    def is_retryable_map_failure(metadata):
        metadata = metadata or {}
        error_type = str(metadata.get("fallback_error_type", "")).lower()
        error_text = str(metadata.get("fallback_error", "")).lower()
        response_text = str(metadata.get("ollama_error_response", "")).lower()

        if (
            "timeout" in error_type
            or "timed out" in error_text
            or "timeout" in error_text
        ):
            return True

        # Context/size-related Ollama failures can often be recovered safely by
        # splitting the source part. Connection/configuration failures should
        # fail fast instead of triggering several useless retries.
        retryable_markers = (
            "context",
            "too large",
            "too long",
            "input length",
            "500 internal server error",
        )
        combined = f"{error_text} {response_text}"
        return any(marker in combined for marker in retryable_markers)

    @classmethod
    def generate_map_part(
        cls,
        document,
        chunk,
        question,
        provider_config,
        label,
    ):
        map_prompt = cls.build_map_prompt(
            document=document,
            chunk=chunk,
            part_label=label,
        )
        return LLMGatewayService.generate_answer(
            question=question,
            retrieved_chunks=[
                {
                    "document_title": document.title,
                    "content": chunk,
                    "chunk_index": label,
                }
            ],
            prompt=map_prompt,
            provider_config=provider_config,
            allow_general_answer=True,
            generation_options={
                "model": ExternalAIModelPolicy.summary_map_model(),
                "max_tokens": int(
                    os.getenv("OLLAMA_DOCUMENT_SUMMARY_MAP_MAX_TOKENS", "240")
                ),
                "timeout": int(
                    os.getenv("OLLAMA_DOCUMENT_SUMMARY_MAP_TIMEOUT_SECONDS", "180")
                ),
                "num_ctx": int(
                    os.getenv("OLLAMA_DOCUMENT_SUMMARY_MAP_NUM_CTX", "4096")
                ),
            },
            allow_extractive_fallback=False,
        )

    @classmethod
    def generate_final_summary(
        cls,
        question,
        prompt,
        coverage,
        provider_config,
        retry=False,
    ):
        max_tokens = cls.summary_max_tokens()
        if retry:
            max_tokens = min(
                max_tokens,
                int(os.getenv("OLLAMA_DOCUMENT_SUMMARY_RETRY_MAX_TOKENS", "900")),
            )

        return LLMGatewayService.generate_answer(
            question=question,
            retrieved_chunks=[
                {
                    "document_title": item["document_title"],
                    "content": "Whole document processed hierarchically.",
                    "chunk_index": None,
                }
                for item in coverage
            ],
            prompt=prompt,
            provider_config=provider_config,
            allow_general_answer=True,
            generation_options={
                "model": ExternalAIModelPolicy.summary_final_model(),
                "max_tokens": max_tokens,
                "timeout": cls.summary_timeout(),
                "num_ctx": cls.summary_num_ctx(),
            },
            allow_extractive_fallback=False,
        )

    @classmethod
    def load_coverage_cache(cls, document, full_text):
        extraction = getattr(document, "text_extraction", None)
        if not extraction:
            return None

        cache = (extraction.metadata or {}).get("external_summary_coverage_cache_v1")
        if not isinstance(cache, dict):
            return None

        expected = cls.coverage_cache_signature(full_text)
        if cache.get("source_hash") != expected:
            return None

        if cache.get("map_model") != ExternalAIModelPolicy.summary_map_model():
            return None

        if cache.get("chunk_char_limit") != int(
            os.getenv("EXTERNAL_AI_SUMMARY_CHUNK_CHAR_LIMIT", "4200")
        ):
            return None

        if cache.get("chunk_overlap") != int(
            os.getenv("EXTERNAL_AI_SUMMARY_CHUNK_OVERLAP", "150")
        ):
            return None

        notes = cache.get("notes")
        if not isinstance(notes, list) or not notes:
            return None

        return cache

    @classmethod
    def save_coverage_cache(
        cls,
        document,
        full_text,
        notes,
        initial_parts,
        effective_parts,
    ):
        extraction = getattr(document, "text_extraction", None)
        if not extraction or not notes:
            return

        metadata = dict(extraction.metadata or {})
        metadata["external_summary_coverage_cache_v1"] = {
            "source_hash": cls.coverage_cache_signature(full_text),
            "map_model": ExternalAIModelPolicy.summary_map_model(),
            "chunk_char_limit": int(
                os.getenv("EXTERNAL_AI_SUMMARY_CHUNK_CHAR_LIMIT", "4200")
            ),
            "chunk_overlap": int(os.getenv("EXTERNAL_AI_SUMMARY_CHUNK_OVERLAP", "150")),
            "initial_parts": int(initial_parts),
            "effective_parts": int(effective_parts),
            "notes": list(notes),
        }

        try:
            extraction.metadata = metadata
            extraction.save(update_fields=["metadata", "updated_at"])
        except Exception as exc:
            # Cache persistence is an optimization only. Never fail a correct
            # summary because a metadata/cache write failed.
            logger.warning(
                "external_document_summary_coverage_cache_write_failed "
                "document=%s error=%s",
                document.uuid,
                exc,
            )

    @staticmethod
    def coverage_cache_signature(full_text):
        return hashlib.sha256((full_text or "").encode("utf-8")).hexdigest()

    @classmethod
    def should_use_single_pass(cls, full_text, question=""):
        if not full_text:
            return False

        char_limit = int(
            os.getenv("EXTERNAL_AI_SUMMARY_SINGLE_PASS_CHAR_LIMIT", "18000")
        )
        if len(full_text) > char_limit:
            return False

        source_tokens = cls.estimate_tokens(full_text)
        cpu_source_token_limit = int(
            os.getenv("EXTERNAL_AI_SUMMARY_SINGLE_PASS_SOURCE_TOKEN_LIMIT", "1400")
        )
        if source_tokens > cpu_source_token_limit:
            return False

        num_ctx = cls.summary_num_ctx()
        output_reserve = cls.summary_max_tokens()
        prompt_overhead = int(
            os.getenv("EXTERNAL_AI_SUMMARY_PROMPT_OVERHEAD_TOKENS", "650")
        )
        safety_ratio = float(
            os.getenv("EXTERNAL_AI_SUMMARY_CONTEXT_SAFETY_RATIO", "0.90")
        )
        estimated_input = source_tokens + cls.estimate_tokens(question)
        available = int(num_ctx * safety_ratio)

        return estimated_input + output_reserve + prompt_overhead <= available

    @staticmethod
    def estimate_tokens(text):
        """Conservative tokenizer-free estimate for mixed prose/table content."""
        text = text or ""
        if not text:
            return 0

        char_estimate = math.ceil(len(text) / 3.0)
        word_estimate = math.ceil(len(text.split()) * 1.45)
        return max(char_estimate, word_estimate, 1)

    @classmethod
    def split_for_summary(cls, text):
        max_chars = int(os.getenv("EXTERNAL_AI_SUMMARY_CHUNK_CHAR_LIMIT", "4200"))
        overlap = int(os.getenv("EXTERNAL_AI_SUMMARY_CHUNK_OVERLAP", "150"))
        return cls.split_text_by_limit(text, max_chars=max_chars, overlap=overlap)

    @classmethod
    def split_text_for_retry(cls, text):
        normalized = cls.normalize_text(text)
        target = max(
            int(os.getenv("EXTERNAL_AI_SUMMARY_RETRY_MIN_CHARS", "2200")),
            math.ceil(len(normalized) / 2),
        )
        return cls.split_text_by_limit(normalized, max_chars=target, overlap=0)

    @classmethod
    def split_text_by_limit(cls, text, max_chars, overlap=0):
        normalized = cls.normalize_text(text)
        if not normalized:
            return []
        if len(normalized) <= max_chars:
            return [normalized]

        chunks = []
        start = 0

        while start < len(normalized):
            end = min(len(normalized), start + max_chars)

            if end < len(normalized):
                # Prefer document/page/table/paragraph boundaries so coverage
                # notes retain coherent sections rather than cutting a row or
                # sentence in half.
                candidates = [
                    normalized.rfind("\n[Page ", start, end),
                    normalized.rfind("\n[Table ", start, end),
                    normalized.rfind("\n\n", start, end),
                    normalized.rfind("\n", start, end),
                ]
                boundary = max(candidates)
                if boundary > start + int(max_chars * 0.55):
                    end = boundary

            chunk = normalized[start:end].strip()
            if chunk:
                chunks.append(chunk)

            if end >= len(normalized):
                break

            next_start = end - max(0, overlap)
            if next_start <= start:
                next_start = end
            start = next_start

        return chunks

    @staticmethod
    def normalize_text(text):
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{4,}", "\n\n\n", text)
        return text.strip()

    @staticmethod
    def get_extracted_text(document):
        extraction = getattr(document, "text_extraction", None)
        return (extraction.extracted_text or "").strip() if extraction else ""

    @staticmethod
    def build_map_prompt(document, chunk, part_label):
        return (
            "You are DocuX External AI preparing loss-resistant coverage notes for a whole-document summary.\n"
            "This is an intermediate coverage step, not the final answer.\n\n"
            "Rules:\n"
            "- Read the entire supplied part.\n"
            "- Preserve every substantive topic, requirement, decision, finding, name, date, number, identifier, exception, caveat, and conclusion present in this part.\n"
            "- For tables/lists, capture their purpose, columns/categories, important values, ranges, totals, outliers, statuses, and notable records.\n"
            "- Do not invent, merge, or silently drop facts.\n"
            "- Keep the notes structured and compact, but never compress away a material fact just to save tokens.\n"
            "- Preserve section/table labels so the final model can trace each fact back to its source area.\n\n"
            f"Document: {document.title}\n"
            f"Coverage part: {part_label}\n\n"
            f"SOURCE PART:\n{chunk}\n\n"
            "Coverage notes:"
        )

    @staticmethod
    def build_final_prompt(question, document_notes):
        notes = "\n\n====================\n\n".join(document_notes)
        return (
            "You are DocuX External AI producing the final answer from complete hierarchical coverage notes.\n\n"
            "Rules:\n"
            "1. Answer the user's actual request, not merely a retrieval query.\n"
            "2. Treat every supplied coverage part as reviewed source material.\n"
            "3. Give a clear executive overview first.\n"
            "4. Then explain all material sections and important points in a logical structure.\n"
            "5. Preserve important names, dates, numbers, identifiers, requirements, exceptions, risks, statuses, findings, and conclusions.\n"
            "6. Explain important table/list trends and notable values when present.\n"
            "7. Do not skip material points just to make the answer shorter.\n"
            "8. Avoid needless repetition, but completeness is more important than brevity when the source is substantial.\n"
            "9. If the user asks for detail or to explain everything, provide detailed section-by-section explanation.\n"
            "10. Clearly distinguish source facts from interpretation and do not invent facts.\n"
            "11. If several documents are present, clearly distinguish them and then provide a combined view where useful.\n\n"
            f"USER REQUEST:\n{question}\n\n"
            f"COMPLETE COVERAGE NOTES:\n{notes}\n\n"
            "Final answer:"
        )

    @staticmethod
    def build_source(document, full_text):
        return {
            "source_type": "external_document",
            "document_uuid": str(document.uuid),
            "document_title": document.title,
            "chunk_index": None,
            "score": 1.0,
            "content_preview": full_text[:1600],
        }

    @staticmethod
    def summary_num_ctx():
        return int(os.getenv("OLLAMA_DOCUMENT_SUMMARY_NUM_CTX", "8192"))

    @staticmethod
    def summary_max_tokens():
        return int(os.getenv("OLLAMA_DOCUMENT_SUMMARY_MAX_TOKENS", "900"))

    @staticmethod
    def summary_timeout():
        return int(os.getenv("OLLAMA_DOCUMENT_SUMMARY_TIMEOUT_SECONDS", "360"))

    @staticmethod
    def failure_result(document, stage, metadata):
        return {
            "answer": (
                "I found the document content, but I could not complete a full, "
                "coverage-checked summary without risking omission. Please retry the request."
            ),
            "sources": [
                {
                    "source_type": "external_document",
                    "document_uuid": str(document.uuid),
                    "document_title": document.title,
                    "chunk_index": None,
                    "score": 1.0,
                    "content_preview": "",
                }
            ],
            "provider": "system",
            "model": "whole_document_summary_guard",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "metadata": {
                "whole_document_summary": True,
                "hierarchical_summary": True,
                "complete": False,
                "failed_stage": stage,
                "generation_metadata": metadata,
            },
        }
