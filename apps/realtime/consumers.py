import uuid

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

from apps.documents.services import DocumentPermissionService
from apps.documents.services.document_processing_event_service import (
    DocumentProcessingEventService,
)


class DocumentEventsConsumer(AsyncJsonWebsocketConsumer):
    """One authenticated socket that can subscribe to many authorized documents."""

    async def connect(self):
        user = self.scope.get("user")
        if (
            not user
            or not user.is_authenticated
            or not getattr(user, "company_id", None)
        ):
            await self.close(code=4401)
            return

        self.document_groups = set()
        await self.accept()
        await self.send_json(
            {
                "event": "connection.ready",
                "event_version": 1,
                "max_document_subscriptions": getattr(
                    settings,
                    "WEBSOCKET_MAX_DOCUMENT_SUBSCRIPTIONS",
                    200,
                ),
            }
        )

    async def disconnect(self, close_code):
        for group_name in list(getattr(self, "document_groups", set())):
            await self.channel_layer.group_discard(group_name, self.channel_name)
        self.document_groups = set()

    async def receive_json(self, content, **kwargs):
        action = str(content.get("action") or "").strip().lower()

        if action == "ping":
            await self.send_json({"event": "pong", "event_version": 1})
            return

        if action == "subscribe":
            await self._subscribe(content.get("document_uuids"))
            return

        if action == "unsubscribe":
            await self._unsubscribe(content.get("document_uuids"))
            return

        await self.send_json(
            {
                "event": "error",
                "event_version": 1,
                "code": "unsupported_action",
                "detail": "Supported actions are subscribe, unsubscribe, and ping.",
            }
        )

    async def _subscribe(self, raw_uuids):
        requested, invalid = self._normalize_uuids(raw_uuids)
        if not requested:
            await self.send_json(
                {
                    "event": "subscription.updated",
                    "event_version": 1,
                    "subscribed_document_uuids": [],
                    "rejected_document_uuids": invalid,
                }
            )
            return

        maximum = getattr(settings, "WEBSOCKET_MAX_DOCUMENT_SUBSCRIPTIONS", 200)
        already_subscribed = [
            item
            for item in requested
            if DocumentProcessingEventService.group_name(item) in self.document_groups
        ]
        new_requests = [item for item in requested if item not in already_subscribed]
        available_slots = max(0, maximum - len(self.document_groups))
        accepted_new = new_requests[:available_slots]
        overflow = new_requests[available_slots:]
        limited = already_subscribed + accepted_new

        documents = await self._authorized_documents(limited)
        authorized = {str(document.uuid): document for document in documents}

        subscribed = []
        rejected = (
            invalid + [item for item in limited if item not in authorized] + overflow
        )

        for document_uuid in limited:
            document = authorized.get(document_uuid)
            if not document:
                continue

            group_name = DocumentProcessingEventService.group_name(document_uuid)
            if group_name not in self.document_groups:
                await self.channel_layer.group_add(group_name, self.channel_name)
                self.document_groups.add(group_name)

            subscribed.append(document_uuid)
            # A snapshot closes the race between upload/processing and subscription,
            # and also restores authoritative state after reconnect.
            await self.send_json(
                DocumentProcessingEventService.snapshot_payload(
                    document,
                    event="document.processing.snapshot",
                )
            )

        await self.send_json(
            {
                "event": "subscription.updated",
                "event_version": 1,
                "subscribed_document_uuids": subscribed,
                "rejected_document_uuids": rejected,
            }
        )

    async def _unsubscribe(self, raw_uuids):
        requested, invalid = self._normalize_uuids(raw_uuids)
        removed = []

        for document_uuid in requested:
            group_name = DocumentProcessingEventService.group_name(document_uuid)
            if group_name not in self.document_groups:
                continue
            await self.channel_layer.group_discard(group_name, self.channel_name)
            self.document_groups.discard(group_name)
            removed.append(document_uuid)

        await self.send_json(
            {
                "event": "subscription.updated",
                "event_version": 1,
                "unsubscribed_document_uuids": removed,
                "rejected_document_uuids": invalid,
            }
        )

    async def document_processing_update(self, event):
        payload = event["payload"]
        document_uuid = payload.get("document_uuid")
        group = DocumentProcessingEventService.group_name(document_uuid)
        if group not in self.document_groups:
            return
        # One bounded authorization lookup per processing-stage event, never a
        # query per extracted field or streaming token. Refresh identity too:
        # the connection's cached User does not reflect account revocation.
        if not await self._event_authorized(document_uuid):
            await self.channel_layer.group_discard(group, self.channel_name)
            self.document_groups.discard(group)
            await self.send_json(
                {
                    "event": "subscription.revoked",
                    "event_version": 1,
                    "document_uuid": document_uuid,
                }
            )
            return
        await self.send_json(payload)

    @database_sync_to_async
    def _event_authorized(self, document_uuid):
        from apps.accounts.models import User

        user = (
            User.objects.select_related("company")
            .filter(
                pk=self.scope["user"].pk,
                company_id=self.scope["user"].company_id,
                is_active=True,
                company__is_deleted=False,
                company__status="active",
            )
            .first()
        )
        return bool(
            user
            and DocumentPermissionService.get_accessible_document_queryset(user)
            .filter(uuid=document_uuid)
            .exists()
        )

    @database_sync_to_async
    def _authorized_documents(self, document_uuids):
        from apps.accounts.models import User

        user = (
            User.objects.select_related("company")
            .filter(
                pk=self.scope["user"].pk,
                company_id=self.scope["user"].company_id,
                is_active=True,
                company__is_deleted=False,
                company__status="active",
            )
            .first()
        )
        if not user:
            return []
        return list(
            DocumentPermissionService.get_accessible_document_queryset(
                user=user,
                required_access="view",
            )
            .filter(uuid__in=document_uuids)
            .select_related("company", "department", "uploaded_by")
            .distinct()
        )

    @staticmethod
    def _normalize_uuids(raw_uuids):
        if not raw_uuids:
            return [], []
        if isinstance(raw_uuids, str):
            raw_uuids = [raw_uuids]

        normalized = []
        invalid = []
        seen = set()
        for value in raw_uuids:
            raw_value = str(value or "").strip()
            try:
                item = str(uuid.UUID(raw_value))
            except (ValueError, TypeError, AttributeError):
                if raw_value and raw_value not in invalid:
                    invalid.append(raw_value)
                continue
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized, invalid
