from django.db import transaction

from apps.vector_search.models import DocumentChunk
from apps.vector_search.services.chunking_service import ChunkingService
from apps.vector_search.services.embedding_provider import EmbeddingProviderFactory


class VectorIndexingService:
    @classmethod
    def index_document(
        cls,
        document,
        provider_name="local_hashing",
        progress_callback=None,
    ):
        extraction = getattr(document, "text_extraction", None)

        if not extraction or not extraction.extracted_text.strip():
            return {
                "indexed": False,
                "chunk_count": 0,
                "reason": "No extracted text available.",
            }

        provider = EmbeddingProviderFactory.get_provider(provider_name)
        chunks = ChunkingService.chunk_text(extraction.extracted_text)

        if progress_callback:
            progress_callback(
                "chunking",
                {
                    "chunk_count": len(chunks),
                },
            )

        chunk_objects = []

        if progress_callback:
            progress_callback(
                "embedding",
                {
                    "chunk_count": len(chunks),
                    "embedding_provider": provider.provider_name,
                    "embedding_dimensions": provider.dimensions,
                },
            )

        # Embedding work can be CPU/network-heavy. Do it before opening the DB
        # transaction so document indexing does not hold a long transaction open.
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

        if progress_callback:
            progress_callback(
                "indexing",
                {
                    "chunk_count": len(chunk_objects),
                    "embedding_provider": provider.provider_name,
                    "embedding_dimensions": provider.dimensions,
                },
            )

        with transaction.atomic():
            DocumentChunk.objects.filter(document=document).delete()
            DocumentChunk.objects.bulk_create(chunk_objects)

            document.is_indexed = bool(chunk_objects)
            document.save(update_fields=["is_indexed", "updated_at"])

        return {
            "indexed": bool(chunk_objects),
            "chunk_count": len(chunk_objects),
            "embedding_provider": provider.provider_name,
            "embedding_dimensions": provider.dimensions,
        }
