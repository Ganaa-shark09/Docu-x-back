from django.test import SimpleTestCase

from apps.ai_engine.services.external_semantic_orchestrator_service import (
    ExternalSemanticOrchestratorService,
)


class ExternalSemanticOrchestratorServiceTests(SimpleTestCase):
    def test_fast_direct_reuses_bounded_answer(self):
        result = ExternalSemanticOrchestratorService._normalize(
            result={
                "action": "direct",
                "clarification": "",
                "fast_answer": "Hello from DocuX External AI.",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "answer_tier": "fast",
                "answer_profile": "general",
                "reasoning_mode": "none",
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["action"], "direct")
        self.assertEqual(result["answer_tier"], "fast")
        self.assertEqual(result["answer_profile"], "general")
        self.assertEqual(result["reasoning_mode"], "none")
        self.assertEqual(result["clarification"], "")
        self.assertEqual(result["fast_answer"], "Hello from DocuX External AI.")

    def test_quality_direct_preserves_reasoning_profile(self):
        result = ExternalSemanticOrchestratorService._normalize(
            result={
                "action": "direct",
                "clarification": "",
                "fast_answer": "This must not be used.",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "answer_tier": "quality",
                "answer_profile": "reasoning",
                "reasoning_mode": "deep",
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["action"], "direct")
        self.assertEqual(result["answer_tier"], "quality")
        self.assertEqual(result["answer_profile"], "reasoning")
        self.assertEqual(result["reasoning_mode"], "deep")
        self.assertEqual(result["fast_answer"], "")

    def test_coding_profile_forces_quality_standard_and_no_fast_answer(self):
        result = ExternalSemanticOrchestratorService._normalize(
            result={
                "action": "direct",
                "clarification": "",
                "fast_answer": "Do not use this.",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "answer_tier": "fast",
                "answer_profile": "coding",
                "reasoning_mode": "deep",
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["answer_tier"], "quality")
        self.assertEqual(result["answer_profile"], "coding")
        self.assertEqual(result["reasoning_mode"], "standard")
        self.assertEqual(result["fast_answer"], "")

    def test_non_direct_actions_never_use_quality_answer_tier(self):
        result = ExternalSemanticOrchestratorService._normalize(
            result={
                "action": "document",
                "clarification": "",
                "fast_answer": "",
                "document_query": "explain the report",
                "web_query": "",
                "document_task": "analyze",
                "answer_tier": "quality",
                "answer_profile": "reasoning",
                "reasoning_mode": "deep",
            },
            has_attached_documents=True,
            has_explicit_documents=True,
            use_web=True,
        )

        self.assertEqual(result["action"], "document")
        self.assertEqual(result["document_task"], "analyze")
        self.assertEqual(result["answer_tier"], "fast")
        self.assertEqual(result["answer_profile"], "general")
        self.assertEqual(result["reasoning_mode"], "none")

    def test_document_without_available_attachment_clarifies(self):
        result = ExternalSemanticOrchestratorService._normalize(
            result={
                "action": "document",
                "clarification": "",
                "fast_answer": "",
                "document_query": "summarize",
                "web_query": "",
                "document_task": "summarize",
                "answer_tier": "fast",
                "answer_profile": "general",
                "reasoning_mode": "none",
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["action"], "clarify")
        self.assertTrue(result["clarification"])
        self.assertEqual(result["document_task"], "none")
        self.assertEqual(result["answer_profile"], "general")

    def test_non_direct_action_discards_fast_answer(self):
        result = ExternalSemanticOrchestratorService._normalize(
            result={
                "action": "web",
                "clarification": "",
                "fast_answer": "This must not leak into the response.",
                "document_query": "",
                "web_query": "latest status",
                "document_task": "none",
                "answer_tier": "fast",
                "answer_profile": "general",
                "reasoning_mode": "none",
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["action"], "web")
        self.assertEqual(result["fast_answer"], "")

    def test_orchestrator_prompt_describes_specialized_profiles(self):
        prompt = ExternalSemanticOrchestratorService._build_system_prompt(
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertIn("semantic router", prompt)
        self.assertIn("fast_answer", prompt)
        self.assertIn("task_domain", prompt)
        self.assertIn("coding", prompt)
        self.assertIn("coding and reasoning are NOT competing categories", prompt)
        self.assertIn("reasoning_mode", prompt)
        self.assertIn("quality_focus", prompt)

    def test_quality_direct_defaults_to_general_standard_when_missing(self):
        result = ExternalSemanticOrchestratorService._normalize(
            result={
                "action": "direct",
                "clarification": "",
                "fast_answer": "",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "answer_tier": "quality",
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )
        self.assertEqual(result["answer_profile"], "general")
        self.assertEqual(result["reasoning_mode"], "standard")


class ExternalSemanticOrchestratorQualityRequirementsTests(SimpleTestCase):
    def test_coding_plan_keeps_task_specific_quality_requirements(self):
        result = ExternalSemanticOrchestratorService._normalize(
            {
                "action": "direct",
                "clarification": "",
                "fast_answer": "",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "answer_tier": "quality",
                "answer_profile": "coding",
                "reasoning_mode": "standard",
                "quality_requirements": [
                    "Use an indivisible check-and-update operation.",
                    "Do not mutate quota in a read-only helper.",
                ],
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["answer_profile"], "coding")
        self.assertEqual(result["answer_tier"], "quality")
        self.assertGreaterEqual(len(result["quality_requirements"]), 3)
        self.assertTrue(
            any(
                "indivisible check-and-update" in item
                for item in result["quality_requirements"]
            )
        )

    def test_non_coding_plan_discards_quality_requirements(self):
        result = ExternalSemanticOrchestratorService._normalize(
            {
                "action": "direct",
                "clarification": "",
                "fast_answer": "hello",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "answer_tier": "fast",
                "answer_profile": "general",
                "reasoning_mode": "none",
                "quality_requirements": ["should disappear"],
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["quality_requirements"], [])


class ExternalSemanticOrchestratorOrthogonalRoutingTests(SimpleTestCase):
    def test_coding_domain_wins_over_deep_reasoning_signal(self):
        result = ExternalSemanticOrchestratorService._normalize(
            {
                "action": "direct",
                "clarification": "",
                "fast_answer": "",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "answer_tier": "quality",
                "task_domain": "coding",
                "reasoning_mode": "deep",
                "quality_focus": ["concurrency", "failure_modes"],
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["task_domain"], "coding")
        self.assertEqual(result["answer_profile"], "coding")
        self.assertEqual(result["answer_tier"], "quality")
        self.assertEqual(result["reasoning_mode"], "standard")
        self.assertEqual(result["quality_focus"], ["concurrency", "failure_modes"])
        self.assertTrue(
            any("atomic" in item for item in result["quality_requirements"])
        )
        self.assertTrue(
            any("fail-open" in item for item in result["quality_requirements"])
        )

    def test_quality_focus_is_bounded_deduplicated_and_validated(self):
        result = ExternalSemanticOrchestratorService._normalize(
            {
                "action": "direct",
                "clarification": "",
                "fast_answer": "",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "answer_tier": "quality",
                "task_domain": "coding",
                "reasoning_mode": "standard",
                "quality_focus": [
                    "api_correctness",
                    "concurrency",
                    "concurrency",
                    "invalid-focus",
                    "failure_modes",
                ],
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(
            result["quality_focus"],
            ["api_correctness", "concurrency", "failure_modes"],
        )

    def test_non_coding_quality_discards_coding_focus(self):
        result = ExternalSemanticOrchestratorService._normalize(
            {
                "action": "direct",
                "clarification": "",
                "fast_answer": "",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "answer_tier": "quality",
                "task_domain": "general",
                "reasoning_mode": "deep",
                "quality_focus": ["concurrency"],
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["answer_profile"], "reasoning")
        self.assertEqual(result["reasoning_mode"], "deep")
        self.assertEqual(result["quality_focus"], [])
        self.assertEqual(result["quality_requirements"], [])


class ExternalSemanticOrchestratorDocumentDependencyTests(SimpleTestCase):
    def test_required_document_dependency_recovers_clarify_when_attachment_exists(self):
        result = ExternalSemanticOrchestratorService._normalize(
            {
                "action": "clarify",
                "clarification": "I cannot inspect the attachment.",
                "fast_answer": "",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "document_dependency": "required",
                "answer_tier": "fast",
                "task_domain": "general",
                "reasoning_mode": "none",
                "quality_focus": [],
            },
            has_attached_documents=True,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["action"], "document")
        self.assertEqual(result["document_dependency"], "required")
        self.assertEqual(result["document_task"], "qa")
        self.assertEqual(result["clarification"], "")

    def test_required_document_dependency_without_document_clarifies(self):
        result = ExternalSemanticOrchestratorService._normalize(
            {
                "action": "direct",
                "clarification": "",
                "fast_answer": "",
                "document_query": "",
                "web_query": "",
                "document_task": "qa",
                "document_dependency": "required",
                "answer_tier": "quality",
                "task_domain": "general",
                "reasoning_mode": "standard",
                "quality_focus": [],
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["action"], "clarify")
        self.assertEqual(result["document_dependency"], "required")
        self.assertTrue(result["clarification"])

    def test_optional_document_dependency_does_not_force_document_route(self):
        result = ExternalSemanticOrchestratorService._normalize(
            {
                "action": "direct",
                "clarification": "",
                "fast_answer": "Hello.",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "document_dependency": "optional",
                "answer_tier": "fast",
                "task_domain": "general",
                "reasoning_mode": "none",
                "quality_focus": [],
            },
            has_attached_documents=True,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(result["action"], "direct")
        self.assertEqual(result["document_dependency"], "optional")
        self.assertEqual(result["fast_answer"], "Hello.")

    def test_prompt_explains_attachment_routing_contract(self):
        prompt = ExternalSemanticOrchestratorService._build_system_prompt(
            has_attached_documents=True,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertIn("document_dependency", prompt)
        self.assertIn("downstream document tool can access them", prompt)
        self.assertIn("Never use clarify only", prompt)
