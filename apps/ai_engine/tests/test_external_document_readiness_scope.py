from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.ai_engine.models import AIConversation, AIConversationDocument
from apps.ai_engine.services.external_document_readiness_service import (
    ExternalDocumentReadinessService,
)
from apps.documents.models import Document
from apps.tenants.models import Company


class ExternalDocumentReadinessScopeTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Scope Co", slug="scope-co")
        self.user = get_user_model().objects.create_user(
            username="scope-user",
            password="test-password",
            company=self.company,
        )
        self.conversation = AIConversation.objects.create(
            company=self.company,
            user=self.user,
            scope=AIConversation.Scope.EXTERNAL,
            title="Scope conversation",
        )
        self.documents = []
        for index in range(2):
            document = Document.objects.create(
                company=self.company,
                uploaded_by=self.user,
                scope=Document.Scope.EXTERNAL,
                title=f"Screenshot {index}.png",
                original_filename=f"Screenshot {index}.png",
                mime_type="image/png",
                status=Document.Status.READY,
                is_ai_ready=True,
                is_ocr_completed=True,
                is_indexed=True,
            )
            AIConversationDocument.objects.create(
                company=self.company,
                conversation=self.conversation,
                document=document,
                uploaded_by=self.user,
            )
            self.documents.append(document)

    def test_all_conversation_uuids_resolve_conversation_scope(self):
        result = ExternalDocumentReadinessService.inspect(
            user=self.user,
            document_uuids=[str(item.uuid) for item in self.documents],
            conversation_uuid=str(self.conversation.uuid),
        )

        self.assertEqual(result["document_scope_mode"], "conversation")
        self.assertEqual(len(result["documents"]), 2)
        self.assertEqual(result["documents"][0]["mime_type"], "image/png")

    def test_subset_resolves_selected_scope(self):
        result = ExternalDocumentReadinessService.inspect(
            user=self.user,
            document_uuids=[str(self.documents[0].uuid)],
            conversation_uuid=str(self.conversation.uuid),
        )

        self.assertEqual(result["document_scope_mode"], "selected")
        self.assertEqual(len(result["documents"]), 1)
