import os
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.document_processing.services.image_understanding_service import (
    ImageUnderstandingService,
)


class ImageUnderstandingServiceTests(SimpleTestCase):
    def test_rich_ocr_uses_fast_vision_tier(self):
        with patch.dict(
            os.environ,
            {"OLLAMA_VISION_FAST_OCR_MIN_CHARS": "20"},
            clear=False,
        ):
            self.assertEqual(
                ImageUnderstandingService.select_model_tier(
                    ocr_text="This screenshot contains enough visible text for OCR."
                ),
                "fast",
            )

    def test_sparse_ocr_keeps_quality_vision_tier(self):
        with patch.dict(
            os.environ,
            {"OLLAMA_VISION_FAST_OCR_MIN_CHARS": "100"},
            clear=False,
        ):
            self.assertEqual(
                ImageUnderstandingService.select_model_tier(ocr_text="Chart"),
                "quality",
            )

    def test_force_quality_overrides_ocr_length(self):
        self.assertEqual(
            ImageUnderstandingService.select_model_tier(
                ocr_text="A" * 1000,
                force_quality=True,
            ),
            "quality",
        )

    def test_long_but_noisy_ocr_keeps_quality_tier(self):
        noisy = ("@@@ *** ~~~ !!! ??? ### " * 30) + "abc 123"

        with patch.dict(
            os.environ,
            {"OLLAMA_VISION_FAST_OCR_MIN_CHARS": "20"},
            clear=False,
        ):
            self.assertEqual(
                ImageUnderstandingService.select_model_tier(ocr_text=noisy),
                "quality",
            )
