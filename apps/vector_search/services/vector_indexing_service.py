from django.db import transaction

from apps.vector_search.models import DocumentChunk
from apps.vector_search.services.chunking_service import ChunkingService
from apps.vector_search.services.embedding_provider import EmbeddingProviderFactory


class VectorIndexingService:
    @classmethod
    @transaction.atomic
    def index_document(cls, document, provider_name="local_hashing"):
        extraction = getattr(document, "text_extraction", None)

        if not extraction or not extraction.extracted_text.strip():
            return {
                "indexed": False,
                "chunk_count": 0,
                "reason": "No extracted text available.",
            }

        provider = EmbeddingProviderFactory.get_provider(provider_name)
        chunks = ChunkingService.chunk_text(extraction.extracted_text)

        DocumentChunk.objects.filter(document=document).delete()

        chunk_objects = []

        for index, chunk in enumerate(chunks):
            embedding = provider.embed_text(chunk["content"])

            chunk_objects.append(
                DocumentChunk(
                    company=document.company,
                    document=document,
                    department=document.department,
                    chunk_index=index,
                    content=chunk["content"],
                    embedding=embedding,
                    character_count=chunk["character_count"],
                    token_count=chunk["token_count"],
                    document_type=document.document_type,
                    sensitivity_label=document.sensitivity_label,
                    embedding_provider=provider.provider_name,
                    metadata={
                        "start_char": chunk["start_char"],
                        "end_char": chunk["end_char"],
                        "source": "text_extraction",
                    },
                )
            )

        DocumentChunk.objects.bulk_create(chunk_objects)

        document.is_indexed = bool(chunk_objects)
        document.save(update_fields=["is_indexed", "updated_at"])

        return {
            "indexed": bool(chunk_objects),
            "chunk_count": len(chunk_objects),
            "embedding_provider": provider.provider_name,
            "embedding_dimensions": provider.dimensions,
        }
