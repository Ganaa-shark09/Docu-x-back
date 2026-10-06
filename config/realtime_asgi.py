import os

from django.core.exceptions import ImproperlyConfigured


if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    raise ImproperlyConfigured("DJANGO_SETTINGS_MODULE must be explicitly configured.")


from django.core.asgi import get_asgi_application  # noqa: E402


# Initialize Django before importing code that may access ORM models.
django_asgi_app = get_asgi_application()


from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import OriginValidator  # noqa: E402
from django.conf import settings  # noqa: E402

from apps.realtime.auth import WebSocketTicketAuthMiddleware  # noqa: E402
from apps.realtime.routing import websocket_urlpatterns  # noqa: E402


application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": OriginValidator(
            WebSocketTicketAuthMiddleware(URLRouter(websocket_urlpatterns)),
            settings.WEBSOCKET_ALLOWED_ORIGINS,
        ),
    }
)
