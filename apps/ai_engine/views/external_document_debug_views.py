from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.documents.models import Document


class ExternalDocumentExtractedTextAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, uuid):
        document = Document.objects.filter(
            uuid=uuid,
            company=request.user.company,
            uploaded_by=request.user,
            scope=Document.Scope.EXTERNAL,
            is_deleted=False,
        ).first()

        if not document:
            return Response(
                {"detail": "External document not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        from apps.documents.services import DocumentPermissionService
        from apps.documents.services.field_permission_service import (
            FieldPermissionService,
        )

        DocumentPermissionService.require_action(request.user, document, "view")
        if FieldPermissionService.has_restrictions(request.user, document, "view"):
            return Response(
                {"detail": "Raw extracted text is protected by field policy."},
                status=403,
            )

        extraction = getattr(document, "text_extraction", None)

        if not extraction:
            return Response(
                {
                    "document_uuid": str(document.uuid),
                    "title": document.title,
                    "status": document.status,
                    "is_ai_ready": document.is_ai_ready,
                    "extracted_text": "",
                    "metadata": {},
                }
            )

        return Response(
            {
                "document_uuid": str(document.uuid),
                "title": document.title,
                "status": document.status,
                "is_ai_ready": document.is_ai_ready,
                "extraction_method": extraction.extraction_method,
                "confidence_score": float(extraction.confidence_score),
                "word_count": extraction.word_count,
                "extracted_text": extraction.extracted_text,
                "metadata": extraction.metadata,
            }
        )
