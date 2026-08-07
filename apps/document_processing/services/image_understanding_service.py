import base64
import os
from pathlib import Path

import requests


class ImageUnderstandingService:
    @classmethod
    def generate_image_summary(cls, image_path, ocr_text=""):
        if os.getenv("OLLAMA_VISION_ENABLED", "True").lower() not in [
            "true",
            "1",
            "yes",
        ]:
            return {
                "summary": "",
                "provider": "disabled",
                "model": "",
                "error": "",
            }

        model = os.getenv("OLLAMA_VISION_MODEL", "llava:latest")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
        timeout = int(os.getenv("OLLAMA_VISION_TIMEOUT_SECONDS", "120"))

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
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                            "images": [image_base64],
                        }
                    ],
                    "options": {
                        "temperature": 0.0,
                        "num_predict": 900,
                    },
                },
                timeout=timeout,
            )
            response.raise_for_status()

            data = response.json()
            summary = data.get("message", {}).get("content", "").strip()

            return {
                "summary": summary,
                "provider": "ollama",
                "model": model,
                "error": "",
            }

        except Exception as exc:
            return {
                "summary": "",
                "provider": "ollama",
                "model": model,
                "error": str(exc),
            }
