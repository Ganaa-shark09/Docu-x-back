import os
import re
from pathlib import Path

from apps.ai_engine.models import AIConversationDocument
from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService


class ExternalDocumentRetrievalService:
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}

    @classmethod
    def retrieve(
        cls,
        user,
        conversation,
        query,
        explicit_document_uuids=None,
        top_k=12,
        document_required=False,
    ):
        documents = cls.get_candidate_documents(
            user=user,
            conversation=conversation,
            explicit_document_uuids=explicit_document_uuids,
        )

        ready_documents = [
            document
            for document in documents
            if document.is_ai_ready and document.status != Document.Status.FAILED
        ]

        processing_documents = [
            document
            for document in documents
            if not document.is_ai_ready and document.status != Document.Status.FAILED
        ]

        failed_documents = [
            document
            for document in documents
            if document.status == Document.Status.FAILED
        ]

        # The semantic orchestrator owns query-to-document routing. Retrieval
        # receives that decision explicitly instead of reclassifying the user's
        # wording with phrase lists or regex shortcuts.
        document_query = bool(document_required)
        chunks = []

        for document in ready_documents:
            extracted_text = cls.get_extracted_text(document)

            if not extracted_text:
                continue

            chunks.extend(
                cls.build_document_context_chunks(
                    document=document,
                    extracted_text=extracted_text,
                    query=query,
                    document_query=document_query,
                )
            )

        chunks.sort(key=lambda item: item["score"], reverse=True)

        if document_query:
            selected_chunks = cls.select_document_chunks_for_document_query(
                chunks=chunks,
                top_k=top_k,
            )
        else:
            selected_chunks = [chunk for chunk in chunks if chunk["score"] >= 2.0][
                :top_k
            ]

        return {
            "chunks": selected_chunks,
            "all_ready_count": len(ready_documents),
            "processing_documents": cls.serialize_documents(processing_documents),
            "failed_documents": cls.serialize_documents(failed_documents),
            "has_sufficient_context": cls.has_sufficient_context(
                query=query,
                chunks=selected_chunks,
                document_query=document_query,
            ),
        }

    @classmethod
    def get_candidate_documents(cls, user, conversation, explicit_document_uuids=None):
        if (
            conversation.company_id != user.company_id
            or conversation.user_id != user.pk
            or conversation.scope != "external"
            or conversation.is_deleted
        ):
            return []
        queryset = Document.objects.filter(
            company=user.company,
            uploaded_by=user,
            scope=Document.Scope.EXTERNAL,
            is_deleted=False,
        )
        permitted = DocumentPermissionService.get_accessible_document_queryset(
            user, scope=Document.Scope.EXTERNAL, ai_policy_only=True
        ).values("pk")
        queryset = queryset.filter(pk__in=permitted)

        if explicit_document_uuids:
            return list(
                queryset.filter(
                    uuid__in=explicit_document_uuids,
                    ai_conversation_attachments__conversation=conversation,
                    ai_conversation_attachments__is_deleted=False,
                )
                .distinct()
                .order_by("created_at")
            )

        attached_document_ids = AIConversationDocument.objects.filter(
            company=user.company,
            conversation=conversation,
            is_deleted=False,
            document__is_deleted=False,
            document__scope=Document.Scope.EXTERNAL,
        ).values_list("document_id", flat=True)

        return list(
            queryset.filter(id__in=attached_document_ids).order_by("created_at")
        )

    @staticmethod
    def get_extracted_text(document):
        extraction = getattr(document, "text_extraction", None)

        if not extraction:
            return ""

        return extraction.extracted_text or ""

    @classmethod
    def build_document_context_chunks(
        cls,
        document,
        extracted_text,
        query,
        document_query,
    ):
        context_limit = int(
            os.getenv("EXTERNAL_AI_DOCUMENT_CONTEXT_CHAR_LIMIT", "18000")
        )
        chunk_limit = int(os.getenv("EXTERNAL_AI_DOCUMENT_CHUNK_CHAR_LIMIT", "6000"))

        normalized_text = cls.normalize_context_text(extracted_text)
        extension = Path(document.original_filename or "").suffix.lower()
        is_image = extension in cls.IMAGE_EXTENSIONS

        header = (
            f"Document title: {document.title}\n"
            f"Original filename: {document.original_filename}\n"
            f"MIME type: {document.mime_type}\n"
            f"Document status: {document.status}\n"
            f"Document is image/screenshot: {is_image}\n"
        )

        # For screenshots/images and short docs, give the full available text as one context block.
        # This avoids losing answer-critical text like “User turn 1”.
        if is_image or len(normalized_text) <= context_limit:
            content = f"{header}\nExtracted content:\n{normalized_text[:context_limit]}"

            return [
                {
                    "source_type": "external_document",
                    "chunk_uuid": None,
                    "document_uuid": str(document.uuid),
                    "governance_revision": getattr(
                        document, "governance_revision", None
                    ),
                    "document_title": document.title,
                    "document_scope": document.scope,
                    "chunk_index": 0,
                    "content": content,
                    "source_preview": normalized_text[:1600],
                    "score": cls.score_chunk(
                        query=query,
                        document=document,
                        chunk_text=content,
                        document_query=document_query,
                    ),
                    "metadata": {
                        "original_filename": document.original_filename,
                        "mime_type": document.mime_type,
                        "status": document.status,
                        "is_ai_ready": document.is_ai_ready,
                        "full_context": True,
                        "is_image": is_image,
                    },
                }
            ]

        chunks = []
        text_chunks = cls.split_text(
            normalized_text, max_chars=chunk_limit, overlap=400
        )

        for index, chunk_text in enumerate(text_chunks):
            content = f"{header}\nExtracted content part {index + 1}:\n{chunk_text}"

            chunks.append(
                {
                    "source_type": "external_document",
                    "chunk_uuid": None,
                    "document_uuid": str(document.uuid),
                    "governance_revision": getattr(
                        document, "governance_revision", None
                    ),
                    "document_title": document.title,
                    "document_scope": document.scope,
                    "chunk_index": index,
                    "content": content,
                    "source_preview": chunk_text[:1600],
                    "score": cls.score_chunk(
                        query=query,
                        document=document,
                        chunk_text=content,
                        document_query=document_query,
                    ),
                    "metadata": {
                        "original_filename": document.original_filename,
                        "mime_type": document.mime_type,
                        "status": document.status,
                        "is_ai_ready": document.is_ai_ready,
                        "full_context": False,
                        "is_image": is_image,
                    },
                }
            )

        return chunks

    @staticmethod
    def normalize_context_text(text):
        text = text or ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    @staticmethod
    def split_text(text, max_chars=6000, overlap=400):
        text = (text or "").strip()

        if not text:
            return []

        if len(text) <= max_chars:
            return [text]

        chunks = []
        start = 0

        while start < len(text):
            end = start + max_chars

            # Prefer splitting at a newline near the boundary.
            boundary = text.rfind("\n", start, end)
            if boundary > start + int(max_chars * 0.6):
                end = boundary

            chunks.append(text[start:end].strip())

            if end >= len(text):
                break

            start = max(0, end - overlap)

        return chunks

    @classmethod
    def select_document_chunks_for_document_query(cls, chunks, top_k):
        if not chunks:
            return []

        # Keep at least one chunk per document, then fill with best remaining chunks.
        selected = []
        seen_documents = set()

        for chunk in chunks:
            document_uuid = chunk["document_uuid"]

            if document_uuid in seen_documents:
                continue

            selected.append(chunk)
            seen_documents.add(document_uuid)

            if len(selected) >= top_k:
                return selected

        for chunk in chunks:
            if chunk in selected:
                continue

            selected.append(chunk)

            if len(selected) >= top_k:
                break

        return selected

    @classmethod
    def score_chunk(cls, query, document, chunk_text, document_query=False):
        query_terms = cls.extract_terms(query)
        content_lower = chunk_text.lower()
        title_lower = (document.title or "").lower()
        filename_lower = (document.original_filename or "").lower()

        score = 0.0

        for term in query_terms:
            if term in content_lower:
                score += 3.0

            if term in title_lower or term in filename_lower:
                score += 2.0

        if document_query:
            # Routing has already established that document evidence is required.
            # Give available document context a uniform baseline and let lexical
            # overlap rank within the authorized request scope.
            score += 4.0

        return round(score, 4)

    @staticmethod
    def extract_terms(query):
        stopwords = {
            "what",
            "is",
            "the",
            "a",
            "an",
            "of",
            "in",
            "to",
            "for",
            "and",
            "or",
            "tell",
            "me",
            "please",
            "can",
            "you",
            "this",
            "that",
            "it",
            "its",
            "about",
            "give",
            "correct",
            "answer",
            "question",
        }

        return {
            term
            for term in re.findall(r"[a-zA-Z0-9]+", query.lower())
            if term not in stopwords and len(term) > 1
        }

    @staticmethod
    def has_sufficient_context(query, chunks, document_query=False):
        if not chunks:
            return False

        if document_query:
            return True

        best_score = max(chunk["score"] for chunk in chunks)

        return best_score >= 2.0

    @staticmethod
    def serialize_documents(documents):
        return [
            {
                "uuid": str(document.uuid),
                "title": document.title,
                "status": document.status,
                "is_ai_ready": document.is_ai_ready,
                "processing_error": document.processing_error,
            }
            for document in documents
        ]
