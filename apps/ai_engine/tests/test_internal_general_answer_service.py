from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engine.services.internal_general_answer_service import (
    InternalGeneralAnswerService,
)


class InternalGeneralAnswerServiceTests(SimpleTestCase):
    @patch("apps.ai_engine.services.internal_general_answer_service.requests.post")
    def test_response_is_generated_by_model_not_phrase_map(self, post):
        post.return_value = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "message": {"content": "Hello! What can I help you with?"},
                "prompt_eval_count": 12,
                "eval_count": 8,
            },
        )
        result = InternalGeneralAnswerService.generate("hello")
        self.assertEqual(result["provider"], "ollama")
        self.assertEqual(result["answer"], "Hello! What can I help you with?")

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["messages"][-1]["content"], "hello")
        self.assertIn("do not invent", payload["messages"][0]["content"].lower())
