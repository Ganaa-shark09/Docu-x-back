import os
import tempfile

from django.test import SimpleTestCase
from docx import Document as DocxDocument
from docx.enum.text import WD_BREAK
from docx.table import Table
from docx.text.paragraph import Paragraph

from apps.document_processing.services.text_extraction_service import (
    TextExtractionService,
)


class TextExtractionServiceTests(SimpleTestCase):
    def test_docx_block_iterator_preserves_paragraph_table_order(self):
        doc = DocxDocument()
        doc.add_paragraph("Before table")
        table = doc.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "A"
        table.rows[0].cells[1].text = "B"
        doc.add_paragraph("After table")

        handle = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
        handle.close()

        try:
            doc.save(handle.name)
            loaded = DocxDocument(handle.name)
            blocks = list(TextExtractionService.iter_docx_blocks(loaded))
        finally:
            os.unlink(handle.name)

        visible = [
            block.text.strip() if isinstance(block, Paragraph) else "TABLE"
            for block in blocks
            if isinstance(block, (Paragraph, Table))
        ]

        self.assertEqual(visible[:3], ["Before table", "TABLE", "After table"])

    def test_docx_page_count_uses_explicit_page_breaks(self):
        doc = DocxDocument()
        doc.add_paragraph("Page one")
        paragraph = doc.add_paragraph()
        paragraph.add_run().add_break(WD_BREAK.PAGE)
        doc.add_paragraph("Page two")

        handle = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
        handle.close()

        try:
            doc.save(handle.name)
            loaded = DocxDocument(handle.name)
            page_count = TextExtractionService.estimate_docx_page_count(loaded)
        finally:
            os.unlink(handle.name)

        self.assertEqual(page_count, 2)
