class PromptBuilderService:
    SYSTEM_INSTRUCTIONS = """
You are DocuX AI, a secure private document assistant.

Rules:
1. Answer only using the authorized document context provided below.
2. If the answer is not available in that context, say that the available company documents do not contain enough information.
3. Do not use outside knowledge to fill gaps and do not invent facts.
4. Be concise, accurate, and business-friendly.
5. Cite the supplied source names, page numbers, or chunk references when useful.
6. Never imply that you searched or used a document that is not present in the supplied context.
""".strip()

    @classmethod
    def build_rag_prompt(
        cls,
        user_question,
        retrieved_chunks,
        document_scope="all",
        selected_document_titles=None,
    ):
        context_blocks = []

        for index, chunk in enumerate(retrieved_chunks, start=1):
            page_number = chunk.get("page_number")
            page_line = f"Page: {page_number}\n" if page_number else ""
            context_blocks.append(
                f"[Source {index}]\n"
                f"Document: {chunk.get('document_title')}\n"
                f"{page_line}"
                f"Chunk: {chunk.get('chunk_index')}\n"
                f"Content:\n{chunk.get('content')}"
            )

        context = "\n\n---\n\n".join(context_blocks)

        if document_scope == "selected":
            selected_titles = [str(title) for title in (selected_document_titles or [])]
            selected_display = (
                ", ".join(selected_titles) or "the explicitly selected documents"
            )
            scope_instruction = (
                "Document scope: SELECTED DOCUMENTS ONLY. The user explicitly selected: "
                f"{selected_display}. Do not answer from or refer to any other company document."
            )
        else:
            scope_instruction = (
                "Document scope: ALL AUTHORIZED INTERNAL DOCUMENTS. The retrieved context "
                "was selected from the user's complete accessible Internal AI library."
            )

        return (
            f"{cls.SYSTEM_INSTRUCTIONS}\n\n"
            f"{scope_instruction}\n\n"
            f"Context:\n{context}\n\n"
            f"User Question:\n{user_question}\n\n"
            f"Answer:"
        )
