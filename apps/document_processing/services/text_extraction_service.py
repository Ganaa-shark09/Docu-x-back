import re
from pathlib import Path

import pdfplumber
import pytesseract
from docx import Document as DocxDocument
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from PIL import Image, ImageEnhance, ImageFilter

from apps.document_processing.models import DocumentTextExtraction
from .image_understanding_service import ImageUnderstandingService


class TextExtractionService:
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
    TEXT_EXTENSIONS = {".txt", ".csv", ".md", ".log"}
    DOCX_EXTENSIONS = {".docx"}
    EXCEL_EXTENSIONS = {".xlsx"}

    @classmethod
    def extract_for_document(cls, document):
        return cls.extract_text(document)

    @classmethod
    def extract_text(cls, document):
        file_path = document.file.path
        extension = Path(file_path).suffix.lower()

        if extension == ".pdf":
            return cls.extract_pdf(document, file_path)

        if extension in cls.IMAGE_EXTENSIONS:
            return cls.extract_image(document, file_path)

        if extension in cls.TEXT_EXTENSIONS:
            return cls.extract_plain_text(document, file_path)

        if extension in cls.DOCX_EXTENSIONS:
            return cls.extract_docx(document, file_path)

        if extension in cls.EXCEL_EXTENSIONS:
            return cls.extract_xlsx(document, file_path)

        return cls.save_extraction(
            document=document,
            text="",
            extraction_method=DocumentTextExtraction.ExtractionMethod.UNKNOWN,
            page_count=0,
            confidence_score=0,
            metadata={"error": f"Unsupported file extension: {extension}"},
        )

    @classmethod
    def extract_pdf(cls, document, file_path):
        pages_text = []

        with pdfplumber.open(file_path) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                page_text = (page.extract_text() or "").strip()
                pages_text.append(f"[Page {page_number}]\n{page_text}".strip())

        text = "\n\n".join(pages_text).strip()

        return cls.save_extraction(
            document=document,
            text=text,
            extraction_method=DocumentTextExtraction.ExtractionMethod.PDF_TEXT,
            page_count=len(pages_text),
            confidence_score=90 if text else 0,
            metadata={"source": "pdfplumber"},
        )

    @classmethod
    def extract_image(cls, document, file_path):
        ocr_text = ""
        ocr_error = ""

        try:
            image = Image.open(file_path)
            prepared_image = cls.prepare_image_for_ocr(image)

            raw_ocr = pytesseract.image_to_string(
                prepared_image,
                config="--psm 6",
            )

            ocr_text = cls.clean_ocr_text(raw_ocr)

        except Exception as exc:
            ocr_error = str(exc)
            ocr_text = ""

        vision_result = ImageUnderstandingService.generate_image_summary(
            image_path=file_path,
            ocr_text=ocr_text,
        )

        raw_visual_summary = (vision_result.get("summary") or "").strip()
        visual_summary = raw_visual_summary
        vision_discarded = False
        vision_discarded_reason = ""

        if ocr_text and raw_visual_summary:
            if not cls.is_visual_summary_compatible_with_ocr(
                visual_summary=raw_visual_summary,
                ocr_text=ocr_text,
            ):
                visual_summary = ""
                vision_discarded = True
                vision_discarded_reason = (
                    "Vision summary had low overlap with OCR text and was discarded "
                    "to avoid hallucinated image interpretation."
                )

        text_parts = [
            "Image/Screenshot analysis:",
            f"File name: {document.original_filename}",
        ]

        if ocr_text:
            text_parts.extend(
                [
                    "",
                    "OCR-visible text. This is the primary source of truth:",
                    ocr_text,
                ]
            )

        if visual_summary:
            text_parts.extend(
                [
                    "",
                    "Visual summary. This is secondary to OCR:",
                    visual_summary,
                ]
            )

        if not ocr_text and not visual_summary:
            text_parts.extend(
                [
                    "",
                    "No reliable OCR text or visual summary could be extracted from this image.",
                ]
            )

        combined_text = "\n".join(text_parts).strip()

        confidence_score = 0
        if ocr_text and visual_summary:
            confidence_score = 88
        elif ocr_text:
            confidence_score = 82
        elif visual_summary:
            confidence_score = 65

        return cls.save_extraction(
            document=document,
            text=combined_text,
            extraction_method=DocumentTextExtraction.ExtractionMethod.OCR,
            page_count=1,
            confidence_score=confidence_score,
            metadata={
                "source": "ocr_first_ollama_vision_secondary",
                "ocr_error": ocr_error,
                "vision_provider": vision_result.get("provider"),
                "vision_model": vision_result.get("model"),
                "vision_model_tier": vision_result.get("model_tier"),
                "vision_wall_duration_ms": vision_result.get("wall_duration_ms", 0),
                "vision_error": vision_result.get("error"),
                "vision_discarded": vision_discarded,
                "vision_discarded_reason": vision_discarded_reason,
                "has_ocr_text": bool(ocr_text),
                "has_visual_summary": bool(visual_summary),
                "raw_visual_summary_preview": raw_visual_summary[:1000],
                "ocr_text_preview": ocr_text[:1000],
            },
        )

    @staticmethod
    def prepare_image_for_ocr(image):
        image = image.convert("RGB")

        width, height = image.size
        scale = 2

        if width < 1800:
            image = image.resize((width * scale, height * scale))

        image = image.convert("L")
        image = ImageEnhance.Contrast(image).enhance(1.8)
        image = ImageEnhance.Sharpness(image).enhance(1.5)
        image = image.filter(ImageFilter.SHARPEN)

        return image

    @classmethod
    def clean_ocr_text(cls, text):
        if not text:
            return ""

        lines = []

        for raw_line in text.splitlines():
            line = raw_line.strip()

            if not line:
                continue

            line = re.sub(r"\s+", " ", line)
            line = line.replace("|", "I") if len(line) < 4 else line

            # Drop obvious OCR garbage lines.
            alpha_numeric_count = len(re.findall(r"[a-zA-Z0-9]", line))
            if alpha_numeric_count < 2:
                continue

            lines.append(line)

        # Remove repeated consecutive OCR lines.
        deduped = []
        previous = None

        for line in lines:
            if line == previous:
                continue

            deduped.append(line)
            previous = line

        return "\n".join(deduped).strip()

    @classmethod
    def is_visual_summary_compatible_with_ocr(cls, visual_summary, ocr_text):
        ocr_terms = cls.extract_keywords(ocr_text)
        visual_terms = cls.extract_keywords(visual_summary)

        if len(ocr_terms) < 8:
            return True

        overlap = ocr_terms.intersection(visual_terms)

        # If OCR is rich but visual summary has almost no overlap,
        # the local vision model likely hallucinated.
        return len(overlap) >= 3

    @staticmethod
    def extract_keywords(text):
        stopwords = {
            "the",
            "and",
            "for",
            "with",
            "this",
            "that",
            "from",
            "there",
            "have",
            "has",
            "are",
            "was",
            "were",
            "you",
            "your",
            "into",
            "about",
            "visible",
            "image",
            "screenshot",
            "screen",
            "text",
            "content",
        }

        return {
            word
            for word in re.findall(r"[a-zA-Z0-9]+", text.lower())
            if len(word) >= 4 and word not in stopwords
        }

    @classmethod
    def extract_plain_text(cls, document, file_path):
        text = Path(file_path).read_text(encoding="utf-8", errors="ignore").strip()

        return cls.save_extraction(
            document=document,
            text=text,
            extraction_method=DocumentTextExtraction.ExtractionMethod.PLAIN_TEXT,
            page_count=1,
            confidence_score=95 if text else 0,
            metadata={"source": "plain_text_reader"},
        )

    @classmethod
    def extract_docx(cls, document, file_path):
        doc = DocxDocument(file_path)
        blocks = []
        table_index = 0

        # python-docx exposes paragraphs and tables as separate collections.
        # Joining all paragraphs first and all tables afterwards destroys the
        # source order, which is particularly harmful for long-document
        # summaries. Walk the document body instead so headings, prose, and
        # tables stay in the same sequence as the original DOCX.
        for block in cls.iter_docx_blocks(doc):
            if isinstance(block, Paragraph):
                value = block.text.strip()
                if not value:
                    continue

                style_name = (getattr(block.style, "name", "") or "").strip()
                if style_name.lower().startswith("heading"):
                    blocks.append(f"[{style_name}] {value}")
                else:
                    blocks.append(value)
                continue

            if isinstance(block, Table):
                table_index += 1
                rows = []

                for row in block.rows:
                    values = [
                        re.sub(r"\s+", " ", cell.text.strip()) for cell in row.cells
                    ]
                    if any(values):
                        rows.append(" | ".join(values))

                if rows:
                    blocks.append(f"[Table {table_index}]\n" + "\n".join(rows))

        text = "\n\n".join(blocks).strip()
        page_count = cls.estimate_docx_page_count(doc)

        return cls.save_extraction(
            document=document,
            text=text,
            extraction_method=DocumentTextExtraction.ExtractionMethod.PLAIN_TEXT,
            page_count=page_count,
            confidence_score=90 if text else 0,
            metadata={
                "source": "python_docx",
                "preserves_block_order": True,
                "table_count": table_index,
                "estimated_page_count": page_count,
            },
        )

    @staticmethod
    def iter_docx_blocks(doc):
        for child in doc.element.body.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, doc)
            elif isinstance(child, CT_Tbl):
                yield Table(child, doc)

    @staticmethod
    def estimate_docx_page_count(doc):
        # DOCX does not store a universally reliable rendered page count.
        # Explicit page breaks are deterministic, however, and give a useful
        # estimate for documents that were authored with page breaks.
        page_breaks = doc.element.body.xpath('.//w:br[@w:type="page"]')
        rendered_breaks = doc.element.body.xpath(".//w:lastRenderedPageBreak")
        return max(1, 1 + len(page_breaks) + len(rendered_breaks))

    @classmethod
    def extract_xlsx(cls, document, file_path):
        workbook = load_workbook(file_path, read_only=True, data_only=True)

        lines = []

        for sheet in workbook.worksheets:
            lines.append(f"Sheet: {sheet.title}")

            for row in sheet.iter_rows(values_only=True):
                values = [str(value).strip() for value in row if value is not None]
                if values:
                    lines.append(" | ".join(values))

        text = "\n".join(lines).strip()

        return cls.save_extraction(
            document=document,
            text=text,
            extraction_method=DocumentTextExtraction.ExtractionMethod.PLAIN_TEXT,
            page_count=len(workbook.worksheets),
            confidence_score=80 if text else 0,
            metadata={"source": "openpyxl"},
        )

    @classmethod
    def save_extraction(
        cls,
        document,
        text,
        extraction_method,
        page_count,
        confidence_score,
        metadata=None,
    ):
        field_names = {
            field.name for field in DocumentTextExtraction._meta.get_fields()
        }

        defaults = {
            "extraction_method": extraction_method,
            "extracted_text": text or "",
            "page_count": page_count,
            "word_count": len((text or "").split()),
            "confidence_score": confidence_score,
            "metadata": metadata or {},
        }

        if "character_count" in field_names:
            defaults["character_count"] = len(text or "")
        elif "char_count" in field_names:
            defaults["char_count"] = len(text or "")

        extraction, _ = DocumentTextExtraction.objects.update_or_create(
            document=document,
            defaults=defaults,
        )

        document.page_count = page_count
        document.is_ocr_completed = bool((text or "").strip())
        document.processing_error = (
            "" if (text or "").strip() else "No text could be extracted."
        )
        document.save(
            update_fields=[
                "page_count",
                "is_ocr_completed",
                "processing_error",
                "updated_at",
            ]
        )

        return extraction
