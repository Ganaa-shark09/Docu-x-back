from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core import signing

from apps.realtime.tickets import WebSocketTicketService


class WebSocketTicketAuthMiddleware:
    """Authenticate browser WebSockets with a short-lived signed ticket.

    Native browser WebSocket clients cannot attach the normal Bearer header used
    by the REST API. The frontend first obtains a short-lived ticket over the
    authenticated HTTP API and then uses only that ticket during the handshake.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        scope["user"] = AnonymousUser()

        raw_query = scope.get("query_string", b"").decode("utf-8", errors="ignore")
        ticket = (parse_qs(raw_query).get("ticket") or [""])[0]

        if ticket:
            try:
                payload = WebSocketTicketService.verify(ticket)
                scope["user"] = await self._load_user(payload)
            except (
                signing.BadSignature,
                signing.SignatureExpired,
                ValueError,
                TypeError,
            ):
                scope["user"] = AnonymousUser()

        return await self.inner(scope, receive, send)

    @staticmethod
    @database_sync_to_async
    def _load_user(payload):
        user_id = payload.get("user_id")
        company_uuid = payload.get("company_uuid")
        if not user_id or not company_uuid:
            return AnonymousUser()

        User = get_user_model()
        user = (
            User.objects.select_related("company")
            .filter(
                pk=user_id,
                company__uuid=company_uuid,
                is_active=True,
            )
            .first()
        )
        return user or AnonymousUser()
