from types import SimpleNamespace
from unittest.mock import patch

from rest_framework.test import APIRequestFactory, force_authenticate
from django.test import SimpleTestCase

from apps.ai_engine.views.external_ai_views import ExternalChatAPIView


class ExternalChatReadinessRoutingTests(SimpleTestCase):
    def setUp(self):
        # Routing tests use service doubles; document authorization is covered
        # separately with persisted messages and source documents.
        visibility = patch(
            "apps.ai_engine.services.message_visibility_service.MessageVisibilityService.visible",
            return_value=True,
        )
        self.visibility = visibility.start()
        self.addCleanup(visibility.stop)
        self.factory = APIRequestFactory()
        self.user = SimpleNamespace(
            is_authenticated=True,
            company_id=1,
            company=SimpleNamespace(pk=1),
        )

    @staticmethod
    def _processing_readiness():
        return {
            "documents": [],
            "ready_documents": [],
            "processing_documents": [{"uuid": "doc-1"}],
            "failed_documents": [],
            "missing_document_uuids": [],
            "all_ready": False,
            "has_processing": True,
            "has_failed": False,
            "document_scope_mode": "selected",
            "conversation_document_uuids": ["doc-1"],
        }

    @patch("apps.ai_engine.views.external_ai_views.ExternalChatService.chat")
    @patch(
        "apps.ai_engine.views.external_ai_views.ExternalSemanticOrchestratorService.plan_resilient"
    )
    @patch(
        "apps.ai_engine.views.external_ai_views.ExternalDocumentReadinessService.inspect"
    )
    def test_unrelated_query_is_not_blocked_by_processing_documents(
        self,
        inspect,
        plan,
        chat,
    ):
        inspect.return_value = {
            **self._processing_readiness(),
            "document_scope_mode": "conversation",
        }
        plan.return_value = {
            "action": "direct",
            "document_dependency": "none",
            "answer_tier": "fast",
            "fast_answer": "Hello.",
        }
        assistant = SimpleNamespace(
            uuid="message-1",
            content="Hello.",
            model_provider="ollama",
            model_name="qwen3:4b-instruct",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        )
        chat.return_value = {
            "conversation": SimpleNamespace(uuid="conversation-1"),
            "user_message": SimpleNamespace(
                metadata={
                    "routing": {
                        "action": "direct",
                        "document_dependency": "none",
                    }
                }
            ),
            "assistant_message": assistant,
            "sources": [],
            "external_chunks": [],
            "web_results": [],
            "processing_documents": [],
            "failed_documents": [],
            "search_warnings": [],
            "intent": "general_question",
        }

        request = self.factory.post(
            "/api/ai/external-chat/",
            {
                "message": "How are you?",
                "document_uuids": ["doc-1"],
                "document_scope_mode": "conversation",
            },
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ExternalChatAPIView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        chat.assert_called_once()
        self.assertEqual(
            chat.call_args.kwargs["routing_plan"]["document_dependency"],
            "none",
        )

    @patch("apps.ai_engine.views.external_ai_views.ExternalChatService.chat")
    @patch(
        "apps.ai_engine.views.external_ai_views.ExternalDocumentReadinessService.inspect"
    )
    def test_ready_document_scope_is_forwarded_to_chat(self, inspect, chat):
        inspect.return_value = {
            "documents": [
                {
                    "uuid": "doc-1",
                    "original_filename": "screen.png",
                    "mime_type": "image/png",
                    "state": "ready",
                }
            ],
            "ready_documents": [{"uuid": "doc-1"}],
            "processing_documents": [],
            "failed_documents": [],
            "missing_document_uuids": [],
            "all_ready": True,
            "has_processing": False,
            "has_failed": False,
            "document_scope_mode": "selected",
            "conversation_document_uuids": ["doc-1", "doc-2"],
        }
        assistant = SimpleNamespace(
            uuid="message-2",
            content="Document answer",
            model_provider="ollama",
            model_name="qwen3:8b",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        )
        chat.return_value = {
            "conversation": SimpleNamespace(uuid="conversation-1"),
            "user_message": SimpleNamespace(
                metadata={
                    "routing": {
                        "action": "document",
                        "document_dependency": "required",
                    }
                }
            ),
            "assistant_message": assistant,
            "sources": [],
            "external_chunks": [],
            "web_results": [],
            "processing_documents": [],
            "failed_documents": [],
            "search_warnings": [],
            "intent": "document",
        }

        request = self.factory.post(
            "/api/ai/external-chat/",
            {
                "message": "Explain the selected file",
                "conversation_uuid": "conversation-1",
                "document_uuids": ["doc-1"],
            },
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ExternalChatAPIView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            chat.call_args.kwargs["document_descriptors"][0]["mime_type"],
            "image/png",
        )
        self.assertEqual(chat.call_args.kwargs["document_scope_mode"], "selected")
        self.assertEqual(
            response.data["external_context"]["routing"]["document_dependency"],
            "required",
        )

    @patch("apps.ai_engine.views.external_ai_views.ExternalChatService.chat")
    @patch(
        "apps.ai_engine.views.external_ai_views.ExternalSemanticOrchestratorService.plan_resilient"
    )
    @patch(
        "apps.ai_engine.views.external_ai_views.ExternalDocumentReadinessService.inspect"
    )
    def test_document_dependent_query_waits_for_processing_documents(
        self,
        inspect,
        plan,
        chat,
    ):
        inspect.return_value = self._processing_readiness()
        plan.return_value = {
            "action": "document",
            "document_dependency": "required",
            "document_task": "analyze",
        }

        request = self.factory.post(
            "/api/ai/external-chat/",
            {
                "message": "Explain this screenshot",
                "document_uuids": ["doc-1"],
                "document_scope_mode": "selected",
            },
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ExternalChatAPIView.as_view()(request)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], "attachments_processing")
        self.assertEqual(response.data["wait_for_document_uuids"], ["doc-1"])
        chat.assert_not_called()

    @patch("apps.ai_engine.views.external_ai_views.ExternalChatService.chat")
    @patch(
        "apps.ai_engine.views.external_ai_views.ExternalSemanticOrchestratorService.plan_resilient"
    )
    @patch(
        "apps.ai_engine.views.external_ai_views.ExternalDocumentReadinessService.inspect"
    )
    def test_document_dependent_query_returns_failed_uuid_for_websocket_ui(
        self,
        inspect,
        plan,
        chat,
    ):
        inspect.return_value = {
            "documents": [{"uuid": "doc-1", "state": "failed"}],
            "ready_documents": [],
            "processing_documents": [],
            "failed_documents": [{"uuid": "doc-1"}],
            "missing_document_uuids": [],
            "all_ready": False,
            "has_processing": False,
            "has_failed": True,
            "document_scope_mode": "selected",
            "conversation_document_uuids": ["doc-1"],
        }
        plan.return_value = {
            "action": "document",
            "document_dependency": "required",
            "document_task": "analyze",
        }

        request = self.factory.post(
            "/api/ai/external-chat/",
            {
                "message": "Explain the selected file",
                "document_uuids": ["doc-1"],
                "document_scope_mode": "selected",
            },
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ExternalChatAPIView.as_view()(request)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["failed_document_uuids"], ["doc-1"])
        chat.assert_not_called()
