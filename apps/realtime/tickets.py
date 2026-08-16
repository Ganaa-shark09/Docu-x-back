from django.conf import settings
from django.core import signing


class WebSocketTicketService:
    SALT = "docux.websocket.ticket.v1"

    @classmethod
    def issue(cls, user):
        payload = {
            "user_id": user.pk,
            "company_uuid": str(user.company.uuid),
        }
        signer = signing.TimestampSigner(salt=cls.SALT)
        return signer.sign_object(payload, compress=True)

    @classmethod
    def verify(cls, ticket):
        signer = signing.TimestampSigner(salt=cls.SALT)
        return signer.unsign_object(
            ticket,
            max_age=getattr(settings, "WEBSOCKET_TICKET_MAX_AGE_SECONDS", 60),
        )
