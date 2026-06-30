import re


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
    def generate_answer(cls, question, retrieved_chunks, prompt=None):
        if not retrieved_chunks:
            return {
                "answer": (
                    "I could not find relevant information in the indexed company documents."
                ),
                "provider": cls.provider_name,
                "model": cls.model_name,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }

        best_sentences = cls._find_relevant_sentences(question, retrieved_chunks)

        if not best_sentences:
            top_chunk = retrieved_chunks[0]
            answer = (
                "The uploaded documents contain relevant information, but I could not "
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

        output = []

        for item in scored_sentences:
            output.append(
                f"{item['sentence']} "
                f"(Source: {item['document_title']}, chunk {item['chunk_index']})"
            )

        return output


class LLMGatewayService:
    @classmethod
    def generate_answer(
        cls, question, retrieved_chunks, prompt=None, provider_name=None
    ):
        # MVP uses local fallback only.
        # Later we can route to OpenAI, Azure OpenAI, Claude, Gemini, Bedrock, or local models.
        return LocalFallbackLLMProvider.generate_answer(
            question=question,
            retrieved_chunks=retrieved_chunks,
            prompt=prompt,
        )
