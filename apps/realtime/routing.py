from django.urls import path

from apps.realtime.consumers import DocumentEventsConsumer


websocket_urlpatterns = [
    path("ws/document-events/", DocumentEventsConsumer.as_asgi()),
]
