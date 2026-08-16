import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.ai_engine.services.internal_document_scope_service import (
    InternalDocumentScopeError,
    InternalDocumentScopeService,
)


class InternalDocumentScopeServiceTests(SimpleTestCase):
    def _ready_queryset(self, rows):
        queryset = MagicMock()
        queryset.count.return_value = len(rows)
        queryset.values_list.return_value = rows
        return queryset

    @patch.object(InternalDocumentScopeService, "get_ai_ready_queryset")
    def test_all_scope_uses_all_accessible_ai_ready_documents(self, get_queryset):
        queryset = self._ready_queryset([])
        queryset.count.return_value = 7
        get_queryset.return_value = queryset

        result = InternalDocumentScopeService.resolve(
            user=SimpleNamespace(),
            document_scope="all",
            document_uuids=[],
        )

        self.assertEqual(result.mode, "all")
        self.assertIsNone(result.search_document_uuids)
        self.assertEqual(result.accessible_document_count, 7)
        self.assertEqual(result.selected_document_count, 0)

    @patch.object(InternalDocumentScopeService, "get_ai_ready_queryset")
    def test_selected_scope_returns_only_requested_accessible_documents(
        self, get_queryset
    ):
        first = uuid.uuid4()
        second = uuid.uuid4()

        accessible = MagicMock()
        accessible.count.return_value = 4
        selected = self._ready_queryset(
            [
                (first, "Policy A"),
                (second, "Policy B"),
            ]
        )
        accessible.filter.return_value = selected
        get_queryset.return_value = accessible

        result = InternalDocumentScopeService.resolve(
            user=SimpleNamespace(),
            document_scope="selected",
            document_uuids=[first, second],
        )

        self.assertEqual(result.mode, "selected")
        self.assertEqual(result.search_document_uuids, [str(first), str(second)])
        self.assertEqual(result.document_titles, ("Policy A", "Policy B"))
        self.assertEqual(result.selected_document_count, 2)

    @patch.object(InternalDocumentScopeService, "get_ai_ready_queryset")
    def test_selected_scope_rejects_missing_or_inaccessible_documents(
        self, get_queryset
    ):
        allowed = uuid.uuid4()
        unavailable = uuid.uuid4()

        accessible = MagicMock()
        accessible.count.return_value = 1
        selected = self._ready_queryset([(allowed, "Allowed")])
        accessible.filter.return_value = selected
        get_queryset.return_value = accessible

        with self.assertRaises(InternalDocumentScopeError) as context:
            InternalDocumentScopeService.resolve(
                user=SimpleNamespace(),
                document_scope="selected",
                document_uuids=[allowed, unavailable],
            )

        self.assertEqual(context.exception.code, "selected_documents_unavailable")
        self.assertEqual(
            context.exception.unavailable_document_uuids,
            [str(unavailable)],
        )
