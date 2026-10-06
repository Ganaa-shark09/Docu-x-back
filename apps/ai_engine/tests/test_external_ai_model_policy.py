import os
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.ai_engine.services.external_ai_model_policy import ExternalAIModelPolicy


class ExternalAIModelPolicyTests(SimpleTestCase):
    def test_default_workload_routing_preserves_quality_model(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                ExternalAIModelPolicy.fast_text_model(), "qwen3:4b-instruct"
            )
            self.assertEqual(ExternalAIModelPolicy.quality_text_model(), "qwen3:8b")
            self.assertEqual(ExternalAIModelPolicy.coding_model(), "qwen2.5-coder:7b")
            self.assertEqual(
                ExternalAIModelPolicy.orchestrator_model(), "qwen3:4b-instruct"
            )
            self.assertEqual(
                ExternalAIModelPolicy.direct_fast_model(), "qwen3:4b-instruct"
            )
            self.assertEqual(ExternalAIModelPolicy.direct_quality_model(), "qwen3:8b")
            self.assertEqual(
                ExternalAIModelPolicy.document_reasoning_model(), "qwen3:8b"
            )
            self.assertEqual(
                ExternalAIModelPolicy.web_answer_model(),
                ExternalAIModelPolicy.fast_text_model(),
            )
            self.assertEqual(
                ExternalAIModelPolicy.summary_map_model(), "qwen3:4b-instruct"
            )
            self.assertEqual(ExternalAIModelPolicy.summary_final_model(), "qwen3:8b")
            self.assertEqual(
                ExternalAIModelPolicy.fast_vision_model(), "qwen3-vl:4b-instruct"
            )
            self.assertEqual(
                ExternalAIModelPolicy.quality_vision_model(), "qwen3-vl:8b"
            )

    def test_direct_model_routes_coding_to_specialized_model(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                ExternalAIModelPolicy.direct_model(
                    answer_tier="quality",
                    answer_profile="coding",
                ),
                "qwen2.5-coder:7b",
            )

    def test_direct_model_routes_general_quality_to_qwen3_8b(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                ExternalAIModelPolicy.direct_model(
                    answer_tier="quality",
                    answer_profile="general",
                ),
                "qwen3:8b",
            )
