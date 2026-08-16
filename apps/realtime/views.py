from django.conf import settings
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.realtime.tickets import WebSocketTicketService


class WebSocketTicketView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not getattr(request.user, "company_id", None):
            return Response(
                {"detail": "User is not assigned to a company."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(
            {
                "ticket": WebSocketTicketService.issue(request.user),
                "expires_in_seconds": settings.WEBSOCKET_TICKET_MAX_AGE_SECONDS,
                "websocket_path": "/ws/document-events/",
            },
            status=status.HTTP_200_OK,
        )
