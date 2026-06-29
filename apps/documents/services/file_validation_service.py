import hashlib
from pathlib import Path

from django.core.exceptions import ValidationError


class DocumentFileValidationService:
    ALLOWED_EXTENSIONS = {
        ".pdf",
        ".docx",
        ".doc",
        ".png",
        ".jpg",
        ".jpeg",
        ".tiff",
        ".txt",
        ".csv",
        ".xlsx",
    }

    ALLOWED_MIME_TYPES = {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "image/png",
        "image/jpeg",
        "image/tiff",
        "text/plain",
        "text/csv",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }

    MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB for MVP

    @classmethod
    def validate_file(cls, uploaded_file):
        if not uploaded_file:
            raise ValidationError("Document file is required.")

        file_name = uploaded_file.name or ""
        extension = Path(file_name).suffix.lower()

        if extension not in cls.ALLOWED_EXTENSIONS:
            raise ValidationError(
                f"Unsupported file type '{extension}'. Allowed types: "
                f"{', '.join(sorted(cls.ALLOWED_EXTENSIONS))}"
            )

        if uploaded_file.size > cls.MAX_FILE_SIZE_BYTES:
            raise ValidationError("File size cannot exceed 50 MB.")

        content_type = getattr(uploaded_file, "content_type", "")

        if content_type and content_type not in cls.ALLOWED_MIME_TYPES:
            raise ValidationError(f"Unsupported MIME type '{content_type}'.")

        return True

    @staticmethod
    def calculate_checksum(uploaded_file):
        sha256 = hashlib.sha256()

        current_position = uploaded_file.tell()

        uploaded_file.seek(0)

        for chunk in uploaded_file.chunks():
            sha256.update(chunk)

        uploaded_file.seek(current_position)

        return sha256.hexdigest()
