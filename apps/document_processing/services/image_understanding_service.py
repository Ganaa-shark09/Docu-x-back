import base64
import logging
import os
import re
import time
from pathlib import Path

import requests

from apps.ai_engine.services.external_ai_model_policy import ExternalAIModelPolicy

logger = logging.getLogger(__name__)


class ImageUnderstandingService:
    @classmethod
    def generate_image_summary(cls, image_path, ocr_text="", force_quality=False):
        if os.getenv("OLLAMA_VISION_ENABLED", "True").lower() not in [
            "true",
            "1",
            "yes",
        ]:
            return {
                "summary": "",
                "provider": "disabled",
                "model": "",
                "model_tier": "disabled",
                "wall_duration_ms": 0,
                "error": "",
            }

        tier = cls.select_model_tier(
            ocr_text=ocr_text,
            force_quality=force_quality,
        )
        model = (
            ExternalAIModelPolicy.fast_vision_model()
            if tier == "fast"
            else ExternalAIModelPolicy.quality_vision_model()
        )

        result = cls._generate_with_model(
            image_path=image_path,
            ocr_text=ocr_text,
            model=model,
            tier=tier,
        )

        # A fast-model infrastructure failure should not make image processing
        # fail when the stronger model is available. Retry once with the quality
        # vision model. We do not retry merely because the wording is different;
        # OCR/source-grounding checks remain responsible for factual validation.
        if result["error"] and tier == "fast":
            quality_model = ExternalAIModelPolicy.quality_vision_model()
            if quality_model != model:
                return cls._generate_with_model(
                    image_path=image_path,
                    ocr_text=ocr_text,
                    model=quality_model,
                    tier="quality_fallback",
                )

        return result

    @classmethod
    def select_model_tier(cls, ocr_text="", force_quality=False):
        if force_quality:
            return "quality"

        normalized = " ".join((ocr_text or "").split())
        min_chars = int(os.getenv("OLLAMA_VISION_FAST_OCR_MIN_CHARS", "250"))

        # Rich, readable OCR means the image is primarily text-bearing. The fast
        # VL model only needs to add layout/visual context. Sparse or noisy OCR
        # leaves more factual burden on vision, so preserve the 8B quality tier.
        if len(normalized) >= min_chars and cls.is_ocr_reliable_for_fast_vision(
            normalized
        ):
            return "fast"

        return "quality"

    @staticmethod
    def is_ocr_reliable_for_fast_vision(ocr_text):
        normalized = " ".join((ocr_text or "").split())
        if not normalized:
            return False

        non_space = [char for char in normalized if not char.isspace()]
        if not non_space:
            return False

        alphanumeric = sum(char.isalnum() for char in non_space)
        alphanumeric_ratio = alphanumeric / len(non_space)
        word_like_tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9._:/%+-]*", normalized)

        return alphanumeric_ratio >= 0.55 and len(word_like_tokens) >= 6

    @classmethod
    def _generate_with_model(cls, image_path, ocr_text, model, tier):
        started_at = time.perf_counter()
        base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
        timeout = int(
            os.getenv(
                "OLLAMA_VISION_FAST_TIMEOUT_SECONDS"
                if tier == "fast"
                else "OLLAMA_VISION_TIMEOUT_SECONDS",
                "120" if tier == "fast" else "240",
            )
        )
        max_tokens = int(
            os.getenv(
                "OLLAMA_VISION_FAST_MAX_TOKENS"
                if tier == "fast"
                else "OLLAMA_VISION_MAX_TOKENS",
                "300" if tier == "fast" else "450",
            )
        )
        num_ctx = int(os.getenv("OLLAMA_VISION_NUM_CTX", "4096"))

        try:
            image_base64 = base64.b64encode(Path(image_path).read_bytes()).decode(
                "utf-8"
            )

            ocr_instruction = ""
            if ocr_text:
                ocr_instruction = (
                    "\nOCR has already detected the following visible text from this image. "
                    "Use this OCR text as the primary source of truth. If your visual interpretation "
                    "conflicts with OCR, prefer OCR and say the visual detail is unclear.\n\n"
                    f"OCR TEXT:\n{ocr_text[:6000]}\n"
                )

            prompt = (
                "You are analyzing an uploaded image for document question answering.\n"
                "Describe only what is visibly present. Do not guess. Do not invent objects.\n"
                "If this is a screenshot, focus on:\n"
                "- application/page title\n"
                "- visible panels or sidebars\n"
                "- uploaded file names\n"
                "- chat messages\n"
                "- modal/dialog content\n"
                "- form fields/buttons\n"
                "- error/status messages\n"
                "- visible timestamps/labels\n"
                "- important text content\n\n"
                "Return a factual structured summary.\n"
                "Do not describe unrelated objects unless clearly visible.\n"
                f"{ocr_instruction}"
            )

            response = requests.post(
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "think": False,
                    "keep_alive": os.getenv("OLLAMA_VISION_KEEP_ALIVE", "10m"),
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                            "images": [image_base64],
                        }
                    ],
                    "options": {
                        "temperature": 0.0,
                        "num_predict": max_tokens,
                        "num_ctx": num_ctx,
                    },
                },
                timeout=timeout,
            )
            response.raise_for_status()

            data = response.json()
            summary = data.get("message", {}).get("content", "").strip()

            wall_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.info(
                "external_image_vision_complete model=%s tier=%s wall_ms=%s "
                "ocr_chars=%s summary_chars=%s",
                model,
                tier,
                wall_duration_ms,
                len(ocr_text or ""),
                len(summary),
            )

            return {
                "summary": summary,
                "provider": "ollama",
                "model": model,
                "model_tier": tier,
                "wall_duration_ms": wall_duration_ms,
                "error": "" if summary else "Vision model returned an empty response.",
            }

        except Exception as exc:
            wall_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.warning(
                "external_image_vision_failed model=%s tier=%s wall_ms=%s error=%s",
                model,
                tier,
                wall_duration_ms,
                exc,
            )
            return {
                "summary": "",
                "provider": "ollama",
                "model": model,
                "model_tier": tier,
                "wall_duration_ms": wall_duration_ms,
                "error": str(exc),
            }
