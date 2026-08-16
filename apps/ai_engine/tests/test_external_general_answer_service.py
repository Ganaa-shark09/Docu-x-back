from django.test import SimpleTestCase

from apps.ai_engine.services.external_general_answer_service import (
    ExternalGeneralAnswerService,
)


class ExternalGeneralAnswerServiceTests(SimpleTestCase):
    def test_quality_prompt_requires_real_concurrency_guarantees(self):
        prompt = ExternalGeneralAnswerService.build_system_prompt(
            intent="general_question",
            quality_mode=True,
            deep_reasoning=True,
            answer_profile="reasoning",
        )

        self.assertIn("Quality-mode requirements", prompt)
        self.assertIn("atomic", prompt)
        self.assertIn("executable code", prompt)
        self.assertIn("Finish every requested section", prompt)
        self.assertIn("extended reasoning internally", prompt)
        self.assertIn("race conditions", prompt)

    def test_coding_prompt_adds_code_specific_validation_contract(self):
        prompt = ExternalGeneralAnswerService.build_system_prompt(
            intent="general_question",
            quality_mode=True,
            answer_profile="coding",
        )

        self.assertIn("Coding-profile requirements", prompt)
        self.assertIn("unused locks", prompt)
        self.assertIn("atomicity", prompt)
        self.assertIn("fail-open", prompt)
        self.assertIn("Current answer profile: coding", prompt)

    def test_fast_prompt_does_not_add_quality_contract(self):
        prompt = ExternalGeneralAnswerService.build_system_prompt(
            intent="general_question",
            quality_mode=False,
            answer_profile="general",
        )

        self.assertNotIn("Quality-mode requirements", prompt)
        self.assertIn("DocuX External AI", prompt)

    def test_deep_quality_enables_thinking(self):
        self.assertIs(
            ExternalGeneralAnswerService.resolve_thinking_mode(
                is_fast_model=False,
                reasoning_mode="deep",
            ),
            True,
        )

    def test_specialized_coding_model_disables_thinking(self):
        self.assertIs(
            ExternalGeneralAnswerService.resolve_thinking_mode(
                is_fast_model=False,
                reasoning_mode="deep",
                supports_thinking=False,
            ),
            False,
        )

    def test_standard_quality_keeps_thinking_disabled(self):
        self.assertIs(
            ExternalGeneralAnswerService.resolve_thinking_mode(
                is_fast_model=False,
                reasoning_mode="standard",
            ),
            False,
        )

    def test_fast_model_never_uses_thinking(self):
        self.assertIs(
            ExternalGeneralAnswerService.resolve_thinking_mode(
                is_fast_model=True,
                reasoning_mode="deep",
            ),
            False,
        )

    def test_coding_prompt_includes_task_specific_acceptance_criteria(self):
        prompt = ExternalGeneralAnswerService.build_system_prompt(
            intent="general_question",
            quality_mode=True,
            answer_profile="coding",
            quality_requirements=[
                "Make the admission check and state update atomic.",
                "A read-only remaining-quota helper must not consume quota.",
            ],
        )

        self.assertIn("Task-specific acceptance criteria", prompt)
        self.assertIn("admission check", prompt)
        self.assertIn("must not consume quota", prompt)
        self.assertIn("API signatures/argument order", prompt)
