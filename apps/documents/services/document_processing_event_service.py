import logging
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

from apps.documents.models import Document

logger = logging.getLogger(__name__)


class DocumentProcessingEventService:
    EVENT_VERSION = 1

    @staticmethod
    def group_name(document_uuid):
        return f"document.{document_uuid}"

    @classmethod
    def transition(
        cls,
        document,
        *,
        stage,
        progress=None,
        status=None,
        message="",
        error=None,
        extra=None,
        publish=True,
    ):
        if stage not in Document.ProcessingStage.values:
            raise ValueError(f"Unsupported document processing stage: {stage}")

        update_fields = ["processing_stage", "updated_at"]
        document.processing_stage = stage

        if progress is not None:
            document.processing_progress = max(0, min(100, int(progress)))
            update_fields.append("processing_progress")

        if status is not None:
            document.status = status
            update_fields.append("status")
        elif stage == Document.ProcessingStage.READY:
            document.status = Document.Status.READY
            update_fields.append("status")
        elif stage == Document.ProcessingStage.FAILED:
            document.status = Document.Status.FAILED
            update_fields.append("status")
        elif stage != Document.ProcessingStage.QUEUED:
            document.status = Document.Status.PROCESSING
            update_fields.append("status")

        if error is not None:
            document.processing_error = str(error)
            update_fields.append("processing_error")
        elif stage != Document.ProcessingStage.FAILED and document.processing_error:
            document.processing_error = ""
            update_fields.append("processing_error")

        # Avoid duplicate update_fields when a caller supplied overlapping state.
        document.save(update_fields=list(dict.fromkeys(update_fields)))

        payload = cls.snapshot_payload(
            document,
            event="document.processing.updated",
            message=message,
            extra=extra,
        )

        if publish:
            transaction.on_commit(lambda: cls.publish_payload(document.uuid, payload))

        return payload

    @classmethod
    def snapshot_payload(cls, document, *, event, message="", extra=None):
        return {
            "event": event,
            "event_version": cls.EVENT_VERSION,
            "event_id": str(uuid.uuid4()),
            "document_uuid": str(document.uuid),
            "scope": document.scope,
            "stage": document.processing_stage,
            "status": document.status,
            "progress": int(document.processing_progress or 0),
            "is_ai_ready": bool(document.is_ai_ready),
            "is_ocr_completed": bool(document.is_ocr_completed),
            "is_indexed": bool(document.is_indexed),
            "message": message or cls.stage_message(document.processing_stage),
            "error": document.processing_error or None,
            "updated_at": document.updated_at.isoformat()
            if document.updated_at
            else None,
            "occurred_at": timezone.now().isoformat(),
            "extra": extra or {},
        }

    @classmethod
    def publish_payload(cls, document_uuid, payload):
        channel_layer = get_channel_layer()
        if channel_layer is None:
            logger.warning(
                "document_processing_event_skipped document=%s reason=no_channel_layer",
                document_uuid,
            )
            return

        try:
            async_to_sync(channel_layer.group_send)(
                cls.group_name(document_uuid),
                {
                    "type": "document_processing_update",
                    "payload": payload,
                },
            )
        except Exception:
            # Realtime delivery is best-effort. Database state is authoritative and
            # document processing must never fail because a browser or Redis channel
            # layer is temporarily unavailable.
            logger.exception(
                "document_processing_event_publish_failed document=%s stage=%s",
                document_uuid,
                payload.get("stage"),
            )

    @staticmethod
    def stage_message(stage):
        return {
            Document.ProcessingStage.QUEUED: "Queued for processing",
            Document.ProcessingStage.EXTRACTING: "Extracting document text",
            Document.ProcessingStage.CLASSIFYING: "Classifying document",
            Document.ProcessingStage.FIREWALL_SCAN: "Running data firewall checks",
            Document.ProcessingStage.CHUNKING: "Creating document chunks",
            Document.ProcessingStage.EMBEDDING: "Creating embeddings",
            Document.ProcessingStage.INDEXING: "Indexing document",
            Document.ProcessingStage.READY: "Document is ready for AI",
            Document.ProcessingStage.FAILED: "Document processing failed",
        }.get(stage, "Document processing updated")
