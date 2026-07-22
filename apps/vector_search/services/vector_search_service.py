from pgvector.django import CosineDistance

from apps.documents.services import DocumentPermissionService
from apps.vector_search.models import DocumentChunk
from apps.vector_search.services.embedding_provider import EmbeddingProviderFactory


class VectorSearchService:
    @classmethod
    def search(
        cls,
        company,
        query,
        user=None,
        department_uuid=None,
        document_type=None,
        sensitivity_label=None,
        top_k=10,
        provider_name="local_hashing",
    ):
        provider = EmbeddingProviderFactory.get_provider(provider_name)
        query_embedding = provider.embed_text(query)

        queryset = (
            DocumentChunk.objects.filter(
                company=company,
                is_deleted=False,
                document__is_deleted=False,
                document__is_ai_ready=True,
            )
            .select_related("document", "department", "company")
            .annotate(distance=CosineDistance("embedding", query_embedding))
        )

        if user:
            allowed_document_ids = (
                DocumentPermissionService.get_accessible_document_queryset(
                    user=user,
                    required_access="view",
                    require_ai=True,
                ).values_list("id", flat=True)
            )

            queryset = queryset.filter(document_id__in=allowed_document_ids)

        if department_uuid:
            queryset = queryset.filter(department__uuid=department_uuid)

        if document_type:
            queryset = queryset.filter(document_type=document_type)

        if sensitivity_label:
            queryset = queryset.filter(sensitivity_label=sensitivity_label)

        results = queryset.order_by("distance")[:top_k]

        output = []

        for chunk in results:
            distance = float(chunk.distance)
            score = max(0.0, 1.0 - distance)

            output.append(
                {
                    "chunk_uuid": str(chunk.uuid),
                    "document_uuid": str(chunk.document.uuid),
                    "document_title": chunk.document.title,
                    "department_uuid": str(chunk.department.uuid)
                    if chunk.department
                    else None,
                    "department_name": chunk.department.name
                    if chunk.department
                    else None,
                    "document_type": chunk.document_type,
                    "sensitivity_label": chunk.sensitivity_label,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "score": round(score, 4),
                    "distance": round(distance, 4),
                    "embedding_provider": chunk.embedding_provider,
                    "metadata": chunk.metadata,
                }
            )

        return output
