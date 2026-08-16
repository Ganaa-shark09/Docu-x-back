import os
import tempfile

from django.test import SimpleTestCase
from docx import Document as DocxDocument

from apps.ai_engine.services.external_document_exact_answer_service import (
    ExternalDocumentExactAnswerService,
)


class ExternalDocumentExactAnswerServiceTests(SimpleTestCase):
    def test_parses_numeric_ordinal_record_request(self):
        result = ExternalDocumentExactAnswerService.parse_positional_request(
            "what's the 14th record of the document?"
        )

        self.assertEqual(result, {"position": 14, "target": "record"})

    def test_docx_uses_one_based_order_and_ignores_smaller_summary_list(self):
        document = DocxDocument()
        document.add_heading("Records", level=1)

        for index in range(1, 16):
            document.add_paragraph(f"Record {index}", style="List Bullet")

        document.add_heading("Summary", level=1)
        document.add_paragraph("Summary point 1", style="List Bullet")
        document.add_paragraph("Summary point 2", style="List Bullet")

        handle = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
        handle.close()

        try:
            document.save(handle.name)
            units = ExternalDocumentExactAnswerService.extract_docx_units(
                file_path=handle.name,
                target="record",
            )
        finally:
            os.unlink(handle.name)

        self.assertEqual(len(units), 15)
        self.assertEqual(units[13], "Record 14")
        self.assertEqual(units[14], "Record 15")
