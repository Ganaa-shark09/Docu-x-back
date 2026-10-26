from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.security.websocket import OriginValidator
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings

from apps.documents.models import Document
from apps.realtime.auth import WebSocketTicketAuthMiddleware
from apps.realtime.routing import websocket_urlpatterns
from apps.realtime.tickets import WebSocketTicketService
from apps.tenants.models import Company


@override_settings(
    CHANNEL_LAYERS={
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    },
    WEBSOCKET_MAX_DOCUMENT_SUBSCRIPTIONS=10,
)
class DocumentEventsConsumerTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.company = Company.objects.create(name="Realtime Co", slug="realtime-ws-co")
        self.other_company = Company.objects.create(
            name="Other Co",
            slug="other-realtime-ws-co",
        )
        User = get_user_model()
        self.user = User.objects.create_user(
            username="ws-user",
            password="test-password",
            company=self.company,
        )
        self.document = Document.objects.create(
            company=self.company,
            uploaded_by=self.user,
            title="allowed.pdf",
            original_filename="allowed.pdf",
            scope=Document.Scope.INTERNAL,
        )
        self.other_document = Document.objects.create(
            company=self.other_company,
            title="blocked.pdf",
            original_filename="blocked.pdf",
            scope=Document.Scope.INTERNAL,
        )

    def test_subscription_is_permission_checked_and_returns_snapshot(self):
        async_to_sync(self._run_subscription_scenario)()

    def test_document_access_revocation_evicts_existing_subscription(self):
        async_to_sync(self._revoked_subscription)(False)

    def test_disabled_account_evicts_existing_subscription(self):
        async_to_sync(self._revoked_subscription)(True)

    async def _revoked_subscription(self, disable_account):
        application = OriginValidator(
            WebSocketTicketAuthMiddleware(URLRouter(websocket_urlpatterns)),
            ["http://localhost:4200"],
        )
        ticket = WebSocketTicketService.issue(self.user)
        communicator = WebsocketCommunicator(
            application,
            f"/ws/document-events/?ticket={ticket}",
            headers=[(b"origin", b"http://localhost:4200")],
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.receive_json_from()
        await communicator.send_json_to(
            {"action": "subscribe", "document_uuids": [str(self.document.uuid)]}
        )
        await communicator.receive_json_from()
        await communicator.receive_json_from()

        @database_sync_to_async
        def revoke():
            if disable_account:
                self.user.__class__.objects.filter(pk=self.user.pk).update(
                    is_active=False
                )
            else:
                Document.objects.filter(pk=self.document.pk).update(uploaded_by=None)

        await revoke()
        event = {
            "type": "document_processing_update",
            "payload": {
                "event": "document.processing.updated",
                "document_uuid": str(self.document.uuid),
                "private_value": "do-not-deliver",
            },
        }
        layer = get_channel_layer()
        await layer.group_send(f"document.{self.document.uuid}", event)
        response = await communicator.receive_json_from()
        self.assertEqual(response["event"], "subscription.revoked")
        self.assertNotIn("do-not-deliver", str(response))
        await layer.group_send(f"document.{self.document.uuid}", event)
        self.assertTrue(await communicator.receive_nothing(timeout=0.1))
        await communicator.disconnect()

    async def _run_subscription_scenario(self):
        application = OriginValidator(
            WebSocketTicketAuthMiddleware(URLRouter(websocket_urlpatterns)),
            ["http://localhost:4200"],
        )
        ticket = WebSocketTicketService.issue(self.user)
        communicator = WebsocketCommunicator(
            application,
            f"/ws/document-events/?ticket={ticket}",
            headers=[(b"origin", b"http://localhost:4200")],
        )

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        ready = await communicator.receive_json_from()
        self.assertEqual(ready["event"], "connection.ready")

        await communicator.send_json_to(
            {
                "action": "subscribe",
                "document_uuids": [
                    str(self.document.uuid),
                    str(self.other_document.uuid),
                ],
            }
        )

        snapshot = await communicator.receive_json_from()
        self.assertEqual(snapshot["event"], "document.processing.snapshot")
        self.assertEqual(snapshot["document_uuid"], str(self.document.uuid))

        subscription = await communicator.receive_json_from()
        self.assertEqual(subscription["event"], "subscription.updated")
        self.assertEqual(
            subscription["subscribed_document_uuids"],
            [str(self.document.uuid)],
        )
        self.assertEqual(
            subscription["rejected_document_uuids"],
            [str(self.other_document.uuid)],
        )

        channel_layer = get_channel_layer()
        await channel_layer.group_send(
            f"document.{self.document.uuid}",
            {
                "type": "document_processing_update",
                "payload": {
                    "event": "document.processing.updated",
                    "event_version": 1,
                    "document_uuid": str(self.document.uuid),
                    "stage": "embedding",
                    "status": "processing",
                    "progress": 70,
                },
            },
        )
        update = await communicator.receive_json_from()
        self.assertEqual(update["event"], "document.processing.updated")
        self.assertEqual(update["document_uuid"], str(self.document.uuid))
        self.assertEqual(update["stage"], "embedding")
        self.assertEqual(update["progress"], 70)

        await communicator.send_json_to(
            {
                "action": "subscribe",
                "document_uuids": ["not-a-uuid"],
            }
        )
        invalid_subscription = await communicator.receive_json_from()
        self.assertEqual(invalid_subscription["event"], "subscription.updated")
        self.assertEqual(
            invalid_subscription["rejected_document_uuids"],
            ["not-a-uuid"],
        )

        await communicator.disconnect()
