class PromptBuilderService:
    SYSTEM_INSTRUCTIONS = """
You are DocuX AI, a secure private document assistant.

Rules:
1. Answer only using the provided context.
2. If the answer is not available in the context, say that the uploaded documents do not contain enough information.
3. Do not invent facts.
4. Be concise and business-friendly.
5. Mention source references when possible.
""".strip()

    @classmethod
    def build_rag_prompt(cls, user_question, retrieved_chunks):
        context_blocks = []

        for index, chunk in enumerate(retrieved_chunks, start=1):
            context_blocks.append(
                f"[Source {index}]\n"
                f"Document: {chunk.get('document_title')}\n"
                f"Chunk: {chunk.get('chunk_index')}\n"
                f"Content:\n{chunk.get('content')}"
            )

        context = "\n\n---\n\n".join(context_blocks)

        return (
            f"{cls.SYSTEM_INSTRUCTIONS}\n\n"
            f"Context:\n{context}\n\n"
            f"User Question:\n{user_question}\n\n"
            f"Answer:"
        )
