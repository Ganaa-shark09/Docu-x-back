import io
import zipfile

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from docx import Document as DocxDocument
from openpyxl import Workbook
from PIL import Image
from pypdf import PdfWriter

from apps.documents.services import (
    DocumentFileValidationService,
)


class DocumentFileValidationServiceTests(SimpleTestCase):
    def test_valid_json_is_accepted_and_rewound(self):
        uploaded = SimpleUploadedFile(
            "finance.json",
            b'[{"gross_amount":"100.01"}]',
            content_type="application/octet-stream",
        )
        self.assertEqual(
            DocumentFileValidationService.validate_file(uploaded), "application/json"
        )
        self.assertEqual(uploaded.tell(), 0)

    def test_json_duplicate_keys_rejected(self):
        uploaded = SimpleUploadedFile("finance.json", b'{"amount":"100","amount":"0"}')
        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)

    def test_json_nonfinite_rejected(self):
        uploaded = SimpleUploadedFile("finance.json", b'{"amount":NaN}')
        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)

    def test_json_primitive_rejected(self):
        uploaded = SimpleUploadedFile("finance.json", b'"plain text"')
        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)

    def test_json_malformed_rejected(self):
        uploaded = SimpleUploadedFile("finance.json", b'{"amount":')
        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)

    def test_json_size_bounded(self):
        uploaded = SimpleUploadedFile(
            "finance.json", b'{"text":"' + b"x" * 8388608 + b'"}'
        )
        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)

    def test_valid_pdf_is_accepted(self):
        buffer = io.BytesIO()

        writer = PdfWriter()
        writer.add_blank_page(
            width=612,
            height=792,
        )
        writer.write(buffer)

        uploaded = SimpleUploadedFile(
            "safe.pdf",
            buffer.getvalue(),
            content_type="text/plain",
        )

        mime = DocumentFileValidationService.validate_file(uploaded)

        self.assertEqual(
            mime,
            "application/pdf",
        )

    def test_fake_pdf_is_rejected_even_when_browser_says_pdf(
        self,
    ):
        uploaded = SimpleUploadedFile(
            "fake.pdf",
            b"This is not a PDF.",
            content_type="application/pdf",
        )

        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)

    def test_valid_docx_is_accepted(self):
        buffer = io.BytesIO()

        document = DocxDocument()
        document.add_paragraph("DocuX secure upload test.")
        document.save(buffer)

        uploaded = SimpleUploadedFile(
            "safe.docx",
            buffer.getvalue(),
            content_type="application/octet-stream",
        )

        mime = DocumentFileValidationService.validate_file(uploaded)

        self.assertEqual(
            mime,
            (
                "application/vnd.openxmlformats-"
                "officedocument.wordprocessingml."
                "document"
            ),
        )

    def test_valid_xlsx_is_accepted(self):
        buffer = io.BytesIO()

        workbook = Workbook()
        sheet = workbook.active
        sheet["A1"] = "DocuX"
        workbook.save(buffer)

        uploaded = SimpleUploadedFile(
            "safe.xlsx",
            buffer.getvalue(),
            content_type="application/octet-stream",
        )

        mime = DocumentFileValidationService.validate_file(uploaded)

        self.assertEqual(
            mime,
            ("application/vnd.openxmlformats-" "officedocument.spreadsheetml." "sheet"),
        )

    def test_zip_path_traversal_is_rejected(self):
        buffer = io.BytesIO()

        with zipfile.ZipFile(
            buffer,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "[Content_Types].xml",
                ('<?xml version="1.0"?>' "<Types></Types>"),
            )

            archive.writestr(
                "word/document.xml",
                "<document/>",
            )

            archive.writestr(
                "../evil.txt",
                "evil",
            )

        uploaded = SimpleUploadedFile(
            "unsafe.docx",
            buffer.getvalue(),
        )

        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)

    def test_suspicious_zip_compression_ratio_is_rejected(
        self,
    ):
        buffer = io.BytesIO()

        with zipfile.ZipFile(
            buffer,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "[Content_Types].xml",
                ('<?xml version="1.0"?>' "<Types></Types>"),
            )

            archive.writestr(
                "word/document.xml",
                "<document/>",
            )

            archive.writestr(
                "word/media/bomb.dat",
                b"A" * (2 * 1024 * 1024),
            )

        uploaded = SimpleUploadedFile(
            "bomb.docx",
            buffer.getvalue(),
        )

        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)

    def test_macro_payload_is_rejected(self):
        buffer = io.BytesIO()

        with zipfile.ZipFile(
            buffer,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "[Content_Types].xml",
                ('<?xml version="1.0"?>' "<Types></Types>"),
            )

            archive.writestr(
                "word/document.xml",
                "<document/>",
            )

            archive.writestr(
                "word/vbaProject.bin",
                b"macro",
            )

        uploaded = SimpleUploadedFile(
            "renamed.docx",
            buffer.getvalue(),
        )

        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)

    def test_valid_image_is_accepted(self):
        buffer = io.BytesIO()

        image = Image.new(
            "RGB",
            (100, 100),
        )

        image.save(
            buffer,
            format="PNG",
        )

        uploaded = SimpleUploadedFile(
            "image.png",
            buffer.getvalue(),
            content_type="text/plain",
        )

        mime = DocumentFileValidationService.validate_file(uploaded)

        self.assertEqual(
            mime,
            "image/png",
        )

    def test_malformed_image_is_rejected(self):
        uploaded = SimpleUploadedFile(
            "image.jpg",
            b"\xff\xd8not-a-real-jpeg",
            content_type="image/jpeg",
        )

        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)

    def test_binary_file_disguised_as_text_is_rejected(
        self,
    ):
        uploaded = SimpleUploadedFile(
            "payload.txt",
            (b"\x00\x01\x02\x03" * 100),
            content_type="text/plain",
        )

        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)

    def test_legacy_doc_is_not_accepted(self):
        uploaded = SimpleUploadedFile(
            "legacy.doc",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
            content_type="application/msword",
        )

        with self.assertRaises(ValidationError):
            DocumentFileValidationService.validate_file(uploaded)
