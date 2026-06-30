from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.vector_search.serializers import (
    VectorSearchRequestSerializer,
    VectorSearchResultSerializer,
)
from apps.vector_search.services import VectorSearchService


class VectorSearchAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not getattr(request.user, "company_id", None):
            return Response(
                {"detail": "User is not assigned to a company."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = VectorSearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        results = VectorSearchService.search(
            company=request.user.company,
            query=serializer.validated_data["query"],
            department_uuid=serializer.validated_data.get("department"),
            document_type=serializer.validated_data.get("document_type"),
            sensitivity_label=serializer.validated_data.get("sensitivity_label"),
            top_k=serializer.validated_data.get("top_k", 10),
        )

        response_serializer = VectorSearchResultSerializer(results, many=True)

        return Response(
            {
                "query": serializer.validated_data["query"],
                "count": len(results),
                "results": response_serializer.data,
            }
        )
