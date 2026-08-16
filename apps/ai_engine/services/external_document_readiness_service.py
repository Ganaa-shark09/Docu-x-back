from apps.documents.models import Document


class ExternalDocumentReadinessService:
    """Authoritative readiness checks for message-scoped external attachments."""

    @classmethod
    def get_documents(cls, user, document_uuids, conversation_uuid=None):
        normalized = cls.normalize_uuids(document_uuids)
        if not normalized:
            return []

        queryset = Document.objects.filter(
            uuid__in=normalized,
            company=user.company,
            scope=Document.Scope.EXTERNAL,
            is_deleted=False,
            ai_conversation_attachments__is_deleted=False,
            ai_conversation_attachments__conversation__user=user,
            ai_conversation_attachments__conversation__company=user.company,
            ai_conversation_attachments__conversation__is_deleted=False,
        )

        if conversation_uuid:
            queryset = queryset.filter(
                ai_conversation_attachments__conversation__uuid=conversation_uuid,
            )

        if not (
            user.is_superuser
            or getattr(user, "role", None) in {"platform_admin", "company_admin"}
        ):
            queryset = queryset.filter(uploaded_by=user)

        return list(queryset.distinct().order_by("created_at"))

    @classmethod
    def get_conversation_document_uuids(cls, user, conversation_uuid):
        if not conversation_uuid:
            return []

        queryset = Document.objects.filter(
            company=user.company,
            scope=Document.Scope.EXTERNAL,
            is_deleted=False,
            ai_conversation_attachments__conversation__uuid=conversation_uuid,
            ai_conversation_attachments__conversation__user=user,
            ai_conversation_attachments__conversation__company=user.company,
            ai_conversation_attachments__conversation__is_deleted=False,
            ai_conversation_attachments__is_deleted=False,
        )

        if not (
            user.is_superuser
            or getattr(user, "role", None) in {"platform_admin", "company_admin"}
        ):
            queryset = queryset.filter(uploaded_by=user)

        return [
            str(item)
            for item in queryset.order_by("created_at")
            .values_list("uuid", flat=True)
            .distinct()
        ]

    @classmethod
    def resolve_scope_mode(cls, user, requested_uuids, conversation_uuid=None):
        requested = cls.normalize_uuids(requested_uuids)
        if not requested:
            return "none", []

        conversation_uuids = cls.get_conversation_document_uuids(
            user=user,
            conversation_uuid=conversation_uuid,
        )
        if conversation_uuids and set(requested) == set(conversation_uuids):
            return "conversation", conversation_uuids

        return "selected", conversation_uuids

    @classmethod
    def inspect(cls, user, document_uuids, conversation_uuid=None):
        requested = cls.normalize_uuids(document_uuids)
        documents = cls.get_documents(
            user=user,
            document_uuids=requested,
            conversation_uuid=conversation_uuid,
        )

        found = {str(document.uuid): document for document in documents}
        missing = [uuid for uuid in requested if uuid not in found]

        scope_mode, conversation_document_uuids = cls.resolve_scope_mode(
            user=user,
            requested_uuids=requested,
            conversation_uuid=conversation_uuid,
        )

        statuses = [cls.serialize_status(document) for document in documents]
        processing = [item for item in statuses if item["state"] == "processing"]
        failed = [item for item in statuses if item["state"] == "failed"]
        ready = [item for item in statuses if item["state"] == "ready"]

        return {
            "documents": statuses,
            "ready_documents": ready,
            "processing_documents": processing,
            "failed_documents": failed,
            "missing_document_uuids": missing,
            "all_ready": bool(requested)
            and not missing
            and len(ready) == len(requested),
            "has_processing": bool(processing),
            "has_failed": bool(failed),
            "document_scope_mode": scope_mode,
            "conversation_document_uuids": conversation_document_uuids,
        }

    @staticmethod
    def normalize_uuids(document_uuids):
        if not document_uuids:
            return []

        if isinstance(document_uuids, str):
            document_uuids = document_uuids.split(",")

        normalized = []
        seen = set()

        for value in document_uuids:
            item = str(value).strip()
            if not item or item in seen:
                continue
            seen.add(item)
            normalized.append(item)

        return normalized

    @staticmethod
    def serialize_status(document):
        raw_status = str(document.status or "").lower()

        if document.processing_error or raw_status == Document.Status.FAILED:
            state = "failed"
        elif document.is_ai_ready and raw_status == Document.Status.READY:
            state = "ready"
        else:
            state = "processing"

        return {
            "uuid": str(document.uuid),
            "title": document.title,
            "original_filename": document.original_filename,
            "mime_type": document.mime_type or "",
            "document_type": document.document_type or "",
            "status": document.status,
            "state": state,
            "processing_stage": document.processing_stage,
            "processing_progress": int(document.processing_progress or 0),
            "is_ai_ready": bool(document.is_ai_ready),
            "is_ocr_completed": bool(document.is_ocr_completed),
            "is_indexed": bool(document.is_indexed),
            "processing_error": document.processing_error or "",
            "updated_at": document.updated_at.isoformat()
            if document.updated_at
            else None,
        }
