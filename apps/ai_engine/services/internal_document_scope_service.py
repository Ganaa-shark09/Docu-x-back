from dataclasses import dataclass
from typing import Iterable

from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService


class InternalDocumentScopeError(Exception):
    def __init__(
        self,
        detail,
        code="invalid_document_scope",
        unavailable_document_uuids=None,
    ):
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.unavailable_document_uuids = unavailable_document_uuids or []


@dataclass(frozen=True)
class ResolvedInternalDocumentScope:
    mode: str
    queryset: object
    document_uuids: tuple[str, ...]
    document_titles: tuple[str, ...]
    accessible_document_count: int
    selected_document_count: int

    @property
    def search_document_uuids(self):
        if self.mode == InternalDocumentScopeService.SELECTED:
            return list(self.document_uuids)
        return None

    def as_metadata(self):
        return {
            "mode": self.mode,
            "document_uuids": list(self.document_uuids),
            "document_titles": list(self.document_titles),
            "accessible_document_count": self.accessible_document_count,
            "selected_document_count": self.selected_document_count,
        }


class InternalDocumentScopeService:
    ALL = "all"
    SELECTED = "selected"
    VALID_MODES = {ALL, SELECTED}

    @classmethod
    def normalize_uuids(cls, values: Iterable | None):
        if not values:
            return []

        if isinstance(values, str):
            values = [part.strip() for part in values.split(",")]

        output = []
        seen = set()

        for value in values:
            normalized = str(value).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            output.append(normalized)

        return output

    @classmethod
    def get_ai_ready_queryset(cls, user):
        return (
            DocumentPermissionService.get_accessible_document_queryset(
                user=user,
                required_access="view",
                require_ai=True,
                scope=Document.Scope.INTERNAL,
            )
            .filter(
                status=Document.Status.READY,
                is_ai_ready=True,
                is_indexed=True,
            )
            .select_related("department", "uploaded_by")
            .distinct()
        )

    @classmethod
    def resolve(cls, user, document_scope=ALL, document_uuids=None):
        mode = (document_scope or cls.ALL).strip().lower()
        normalized_uuids = cls.normalize_uuids(document_uuids)

        if mode not in cls.VALID_MODES:
            raise InternalDocumentScopeError(
                "document_scope must be either 'all' or 'selected'.",
                code="invalid_document_scope",
            )

        if mode == cls.ALL and normalized_uuids:
            raise InternalDocumentScopeError(
                "document_uuids must be empty when document_scope is 'all'.",
                code="document_scope_mismatch",
            )

        if mode == cls.SELECTED and not normalized_uuids:
            raise InternalDocumentScopeError(
                (
                    "At least one document UUID is required when "
                    "document_scope is 'selected'."
                ),
                code="selected_documents_required",
            )

        accessible_queryset = cls.get_ai_ready_queryset(user)
        accessible_document_count = accessible_queryset.count()

        if mode == cls.ALL:
            return ResolvedInternalDocumentScope(
                mode=mode,
                queryset=accessible_queryset,
                document_uuids=tuple(),
                document_titles=tuple(),
                accessible_document_count=accessible_document_count,
                selected_document_count=0,
            )

        selected_queryset = accessible_queryset.filter(uuid__in=normalized_uuids)
        selected_rows = list(selected_queryset.values_list("uuid", "title"))
        found_by_uuid = {str(uuid): title for uuid, title in selected_rows}

        unavailable = [
            document_uuid
            for document_uuid in normalized_uuids
            if document_uuid not in found_by_uuid
        ]

        if unavailable:
            raise InternalDocumentScopeError(
                "One or more selected documents are unavailable for Internal AI. "
                "They may be missing, inaccessible, not fully processed, or not indexed.",
                code="selected_documents_unavailable",
                unavailable_document_uuids=unavailable,
            )

        ordered_titles = tuple(found_by_uuid[uuid] for uuid in normalized_uuids)

        return ResolvedInternalDocumentScope(
            mode=mode,
            queryset=selected_queryset,
            document_uuids=tuple(normalized_uuids),
            document_titles=ordered_titles,
            accessible_document_count=accessible_document_count,
            selected_document_count=len(normalized_uuids),
        )
