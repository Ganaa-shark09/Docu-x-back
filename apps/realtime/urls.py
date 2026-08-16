from django.urls import path

from apps.realtime.views import WebSocketTicketView


urlpatterns = [
    path(
        "websocket-ticket/",
        WebSocketTicketView.as_view(),
        name="realtime-websocket-ticket",
    ),
]
