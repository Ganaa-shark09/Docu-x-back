from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engine.services.external_semantic_orchestrator_service import (
    ExternalSemanticOrchestratorService,
)


class ExternalSemanticOrchestratorRecoveryTests(SimpleTestCase):
    @patch.object(ExternalSemanticOrchestratorService, "plan")
    def test_clarify_with_available_documents_retries_once_without_history(self, plan):
        plan.side_effect = [
            {
                "action": "clarify",
                "document_dependency": "none",
                "metadata": {"history_message_count": 2},
            },
            {
                "action": "document",
                "document_dependency": "required",
                "document_task": "analyze",
                "metadata": {"history_message_count": 0},
            },
        ]

        descriptors = [
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "original_filename": "Screenshot 2026-08-07.png",
                "mime_type": "image/png",
                "state": "ready",
            }
        ]

        result = ExternalSemanticOrchestratorService.plan_resilient(
            message="tell me about this screenshot",
            has_attached_documents=True,
            has_explicit_documents=True,
            document_descriptors=descriptors,
            document_scope_mode="selected",
        )

        self.assertEqual(result["action"], "document")
        self.assertEqual(result["document_dependency"], "required")
        self.assertTrue(result["metadata"]["routing_recovery"]["performed"])
        self.assertEqual(plan.call_count, 2)
        self.assertTrue(plan.call_args_list[1].kwargs["ignore_history"])

    @patch.object(ExternalSemanticOrchestratorService, "plan")
    def test_direct_unrelated_query_does_not_trigger_recovery(self, plan):
        plan.return_value = {
            "action": "direct",
            "document_dependency": "none",
            "fast_answer": "Hello.",
        }

        result = ExternalSemanticOrchestratorService.plan_resilient(
            message="how are you?",
            has_attached_documents=True,
            has_explicit_documents=False,
            document_descriptors=[{"uuid": "doc", "mime_type": "image/png"}],
            document_scope_mode="conversation",
        )

        self.assertEqual(result["action"], "direct")
        plan.assert_called_once()

    def test_prompt_contains_document_descriptors_and_scope_semantics(self):
        prompt = ExternalSemanticOrchestratorService._build_system_prompt(
            has_attached_documents=True,
            has_explicit_documents=True,
            use_web=True,
            document_scope_mode="selected",
            document_descriptors=[
                {
                    "uuid": "11111111-1111-1111-1111-111111111111",
                    "original_filename": "Screenshot 2026-08-07.png",
                    "mime_type": "image/png",
                    "state": "ready",
                }
            ],
        )

        self.assertIn("document_scope_mode=selected", prompt)
        self.assertIn("documents_selected=True", prompt)
        self.assertIn("Screenshot 2026-08-07.png", prompt)
        self.assertIn("image/png", prompt)
        self.assertIn("conversation: no subset was selected", prompt)


class ExternalSemanticSelectedScopeNormalizationTests(SimpleTestCase):
    def test_selected_scope_cannot_be_downgraded_to_direct(self):
        result = ExternalSemanticOrchestratorService._normalize(
            result={
                "action": "direct",
                "clarification": "",
                "fast_answer": "Generic answer",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "document_dependency": "none",
                "answer_tier": "fast",
                "task_domain": "general",
                "reasoning_mode": "none",
                "quality_focus": [],
            },
            has_attached_documents=True,
            has_explicit_documents=True,
            use_web=True,
        )

        self.assertEqual(result["action"], "document")
        self.assertEqual(result["document_dependency"], "required")
        self.assertEqual(result["document_task"], "qa")
        self.assertEqual(result["fast_answer"], "")

    def test_selected_scope_preserves_model_document_task(self):
        result = ExternalSemanticOrchestratorService._normalize(
            result={
                "action": "direct",
                "clarification": "",
                "fast_answer": "",
                "document_query": "summarize selected file",
                "web_query": "",
                "document_task": "summarize",
                "document_dependency": "none",
                "answer_tier": "quality",
                "task_domain": "general",
                "reasoning_mode": "standard",
                "quality_focus": [],
            },
            has_attached_documents=True,
            has_explicit_documents=True,
            use_web=True,
        )

        self.assertEqual(result["action"], "document")
        self.assertEqual(result["document_dependency"], "required")
        self.assertEqual(result["document_task"], "summarize")
