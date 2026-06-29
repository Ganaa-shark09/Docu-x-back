from pathlib import Path

import pypdf
import pytesseract
from PIL import Image

from apps.document_processing.models import DocumentTextExtraction


class TextExtractionService:
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}
    TEXT_EXTENSIONS = {".txt", ".csv"}

    @classmethod
    def extract_for_document(cls, document):
        file_path = Path(document.file.path)
        extension = file_path.suffix.lower()

        if extension == ".pdf":
            result = cls._extract_pdf_text(file_path)
        elif extension in cls.IMAGE_EXTENSIONS:
            result = cls._extract_image_text(file_path)
        elif extension in cls.TEXT_EXTENSIONS:
            result = cls._extract_plain_text(file_path)
        else:
            result = {
                "text": "",
                "page_count": 0,
                "method": DocumentTextExtraction.ExtractionMethod.UNKNOWN,
                "confidence": 0,
                "metadata": {
                    "warning": f"Text extraction not implemented for {extension}",
                },
            }

        text = result["text"] or ""

        extraction, _ = DocumentTextExtraction.objects.update_or_create(
            document=document,
            defaults={
                "extraction_method": result["method"],
                "extracted_text": text,
                "page_count": result["page_count"],
                "character_count": len(text),
                "word_count": len(text.split()),
                "confidence_score": result["confidence"],
                "metadata": result.get("metadata", {}),
            },
        )

        document.page_count = extraction.page_count
        document.is_ocr_completed = bool(text.strip())
        document.processing_error = ""
        document.save(
            update_fields=[
                "page_count",
                "is_ocr_completed",
                "processing_error",
                "updated_at",
            ]
        )

        return extraction

    @staticmethod
    def _extract_pdf_text(file_path):
        text_parts = []
        page_count = 0

        with open(file_path, "rb") as file_obj:
            reader = pypdf.PdfReader(file_obj)
            page_count = len(reader.pages)

            for page in reader.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)

        text = "\n\n".join(text_parts).strip()

        confidence = 90 if text else 20

        return {
            "text": text,
            "page_count": page_count,
            "method": DocumentTextExtraction.ExtractionMethod.PDF_TEXT,
            "confidence": confidence,
            "metadata": {
                "parser": "pypdf",
            },
        }

    @staticmethod
    def _extract_image_text(file_path):
        image = Image.open(file_path)
        text = pytesseract.image_to_string(image).strip()

        confidence = 75 if text else 20

        return {
            "text": text,
            "page_count": 1,
            "method": DocumentTextExtraction.ExtractionMethod.OCR,
            "confidence": confidence,
            "metadata": {
                "parser": "pytesseract",
            },
        }

    @staticmethod
    def _extract_plain_text(file_path):
        text = file_path.read_text(encoding="utf-8", errors="ignore").strip()

        return {
            "text": text,
            "page_count": 1,
            "method": DocumentTextExtraction.ExtractionMethod.PLAIN_TEXT,
            "confidence": 95 if text else 20,
            "metadata": {
                "parser": "plain_text",
            },
        }
