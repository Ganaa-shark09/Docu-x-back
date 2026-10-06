import re


class InternalDocumentRelevanceService:
    """Filter weak hashing-embedding matches before Internal AI RAG.

    The current local hashing embedding is lexical rather than semantic. Hash
    collisions can therefore make unrelated short messages retrieve arbitrary
    chunks. This service requires real token overlap for the broad "all
    documents" scope. Explicitly selected documents bypass the overlap gate,
    because phrases such as "explain this" intentionally refer to the selected
    artifact even when they share no words with its content.
    """

    TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
    STOPWORDS = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "that",
        "the",
        "this",
        "to",
        "us",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "would",
        "you",
        "your",
    }

    @classmethod
    def filter_relevant_chunks(
        cls,
        question,
        retrieved_chunks,
        force_document_scope=False,
    ):
        chunks = list(retrieved_chunks or [])
        if force_document_scope:
            return chunks

        query_terms = cls.meaningful_terms(question)
        if not query_terms:
            return []

        relevant = []
        for chunk in chunks:
            haystack = " ".join(
                [
                    str(chunk.get("document_title") or ""),
                    str(chunk.get("document_filename") or ""),
                    str(chunk.get("content") or ""),
                ]
            )
            haystack_terms = cls.all_terms(haystack)
            overlap = query_terms.intersection(haystack_terms)
            if not overlap:
                continue

            enriched = dict(chunk)
            enriched.setdefault("metadata", {})
            enriched["metadata"] = dict(enriched["metadata"] or {})
            enriched["metadata"]["internal_query_overlap_terms"] = sorted(overlap)
            relevant.append(enriched)

        return relevant

    @classmethod
    def meaningful_terms(cls, value):
        return {
            token
            for token in cls.all_terms(value)
            if len(token) >= 3 and token not in cls.STOPWORDS
        }

    @classmethod
    def all_terms(cls, value):
        return {
            token.lower()
            for token in cls.TOKEN_PATTERN.findall(str(value or "").lower())
        }
