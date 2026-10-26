import hashlib
import json
import warnings
import zipfile

from pathlib import Path, PurePosixPath

import magic

from django.conf import settings
from django.core.exceptions import ValidationError

from PIL import Image

from pypdf import PdfReader


class DocumentFileValidationService:
    """
    Security boundary for all document uploads.

    Never trust:
      - browser supplied content_type
      - filename extension alone
      - ZIP/Office archive metadata alone

    validate_file() returns the canonical MIME type that should be
    persisted with the Document.
    """

    READ_SAMPLE_BYTES = 64 * 1024

    ALLOWED_EXTENSIONS = {
        ".pdf",
        ".docx",
        ".png",
        ".jpg",
        ".jpeg",
        ".tif",
        ".tiff",
        ".txt",
        ".csv",
        ".json",
        ".xlsx",
    }

    CANONICAL_MIME_BY_EXTENSION = {
        ".pdf": "application/pdf",
        ".docx": (
            "application/vnd.openxmlformats-officedocument." "wordprocessingml.document"
        ),
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".json": "application/json",
        ".xlsx": (
            "application/vnd.openxmlformats-officedocument." "spreadsheetml.sheet"
        ),
    }

    MAGIC_MIME_BY_EXTENSION = {
        ".pdf": {
            "application/pdf",
        },
        ".docx": {
            "application/zip",
            "application/x-zip-compressed",
            (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        },
        ".xlsx": {
            "application/zip",
            "application/x-zip-compressed",
            ("application/vnd.openxmlformats-officedocument." "spreadsheetml.sheet"),
        },
        ".png": {
            "image/png",
        },
        ".jpg": {
            "image/jpeg",
        },
        ".jpeg": {
            "image/jpeg",
        },
        ".tif": {
            "image/tiff",
        },
        ".tiff": {
            "image/tiff",
        },
        ".txt": {
            "text/plain",
            "text/x-plain",
        },
        ".csv": {
            "text/plain",
            "text/csv",
            "application/csv",
            "text/x-csv",
        },
        ".json": {"application/json", "text/plain"},
    }

    OFFICE_REQUIRED_MEMBERS = {
        ".docx": {
            "[Content_Types].xml",
            "word/document.xml",
        },
        ".xlsx": {
            "[Content_Types].xml",
            "xl/workbook.xml",
        },
    }

    OFFICE_FORBIDDEN_PARTS = (
        "vbaproject.bin",
        "vbadata.xml",
        "/embeddings/",
        "customui/",
    )

    OFFICE_FORBIDDEN_CONTENT_MARKERS = (
        b"macroenabled",
        b"vbaproject",
    )

    @classmethod
    def validate_file(cls, uploaded_file):
        if not uploaded_file:
            raise ValidationError("Document file is required.")

        cls._validate_filename(uploaded_file)

        file_size = cls._get_file_size(uploaded_file)

        if file_size <= 0:
            raise ValidationError("Empty files are not allowed.")

        max_size = getattr(
            settings,
            "DOCUMENT_UPLOAD_MAX_FILE_SIZE_BYTES",
            50 * 1024 * 1024,
        )

        if file_size > max_size:
            raise ValidationError("File size exceeds the configured upload limit.")

        extension = Path(uploaded_file.name).suffix.lower()

        detected_mime = cls._detect_mime_type(uploaded_file)

        cls._validate_detected_mime(
            extension=extension,
            detected_mime=detected_mime,
        )

        if extension == ".pdf":
            cls._validate_pdf(uploaded_file)

        elif extension in {
            ".docx",
            ".xlsx",
        }:
            cls._validate_office_archive(
                uploaded_file,
                extension,
            )

        elif extension in {
            ".png",
            ".jpg",
            ".jpeg",
            ".tif",
            ".tiff",
        }:
            cls._validate_image(uploaded_file)

        elif extension in {
            ".txt",
            ".csv",
        }:
            cls._validate_text_file(uploaded_file)
        elif extension == ".json":
            cls._validate_json_file(uploaded_file)

        canonical_mime = cls.CANONICAL_MIME_BY_EXTENSION[extension]

        setattr(
            uploaded_file,
            "_docux_detected_mime_type",
            canonical_mime,
        )

        cls._rewind(uploaded_file)

        return canonical_mime

    @classmethod
    def get_detected_mime_type(
        cls,
        uploaded_file,
    ):
        detected = getattr(
            uploaded_file,
            "_docux_detected_mime_type",
            None,
        )

        if detected:
            return detected

        return cls.validate_file(uploaded_file)

    @classmethod
    def _validate_filename(
        cls,
        uploaded_file,
    ):
        file_name = str(uploaded_file.name or "").strip()

        if not file_name:
            raise ValidationError("A filename is required.")

        if "\x00" in file_name:
            raise ValidationError("Filename contains invalid characters.")

        if len(file_name) > 255:
            raise ValidationError("Filename is too long.")

        normalized = file_name.replace(
            "\\",
            "/",
        )

        if "/" in normalized:
            raise ValidationError("Filename must not contain directory paths.")

        extension = Path(file_name).suffix.lower()

        if extension not in cls.ALLOWED_EXTENSIONS:
            allowed = ", ".join(sorted(cls.ALLOWED_EXTENSIONS))

            raise ValidationError(
                f"Unsupported file type " f"'{extension}'. " f"Allowed types: {allowed}"
            )

    @staticmethod
    def _get_file_size(
        uploaded_file,
    ):
        size = getattr(
            uploaded_file,
            "size",
            None,
        )

        if size is not None:
            return int(size)

        current_position = uploaded_file.tell()

        uploaded_file.seek(
            0,
            2,
        )

        size = uploaded_file.tell()

        uploaded_file.seek(current_position)

        return size

    @classmethod
    def _detect_mime_type(
        cls,
        uploaded_file,
    ):
        current_position = uploaded_file.tell()

        try:
            uploaded_file.seek(0)

            sample = uploaded_file.read(cls.READ_SAMPLE_BYTES)

            if not sample:
                raise ValidationError("Empty files are not allowed.")

            try:
                detected = magic.from_buffer(
                    sample,
                    mime=True,
                )
            except Exception as exc:
                raise ValidationError("Unable to inspect the uploaded file.") from exc

            return (
                (detected or "")
                .split(
                    ";",
                    1,
                )[0]
                .strip()
                .lower()
            )

        finally:
            uploaded_file.seek(current_position)

    @classmethod
    def _validate_detected_mime(
        cls,
        extension,
        detected_mime,
    ):
        allowed = cls.MAGIC_MIME_BY_EXTENSION[extension]

        if detected_mime not in allowed:
            raise ValidationError(
                (
                    "File content does not match "
                    f"the '{extension}' extension. "
                    f"Detected type: "
                    f"'{detected_mime or 'unknown'}'."
                )
            )

    @classmethod
    def _validate_pdf(
        cls,
        uploaded_file,
    ):
        current_position = uploaded_file.tell()

        try:
            uploaded_file.seek(0)

            try:
                reader = PdfReader(
                    uploaded_file,
                    strict=False,
                )

                if reader.is_encrypted:
                    raise ValidationError("Encrypted PDFs are not supported.")

                page_count = len(reader.pages)

            except ValidationError:
                raise

            except Exception as exc:
                raise ValidationError("The PDF is malformed or unreadable.") from exc

            max_pages = getattr(
                settings,
                "DOCUMENT_UPLOAD_MAX_PDF_PAGES",
                1000,
            )

            if page_count > max_pages:
                raise ValidationError(
                    ("PDF contains too many pages " "for safe processing.")
                )

            root = cls._resolve_pdf_object(reader.trailer.get("/Root"))

            if not root:
                return

            if root.get("/OpenAction") is not None or root.get("/AA") is not None:
                raise ValidationError(
                    ("PDF contains active actions " "that are not allowed.")
                )

            names = cls._resolve_pdf_object(root.get("/Names"))

            if names:
                if names.get("/JavaScript") is not None:
                    raise ValidationError(("PDF JavaScript is " "not allowed."))

                if names.get("/EmbeddedFiles") is not None:
                    raise ValidationError(("PDF embedded files are " "not allowed."))

        finally:
            uploaded_file.seek(current_position)

    @staticmethod
    def _resolve_pdf_object(
        value,
    ):
        if value is None:
            return None

        get_object = getattr(
            value,
            "get_object",
            None,
        )

        if callable(get_object):
            try:
                return get_object()
            except Exception:
                return value

        return value

    @classmethod
    def _validate_office_archive(
        cls,
        uploaded_file,
        extension,
    ):
        current_position = uploaded_file.tell()

        try:
            uploaded_file.seek(0)

            try:
                with zipfile.ZipFile(
                    uploaded_file,
                    "r",
                ) as archive:
                    cls._validate_zip_members(archive)

                    member_names = {info.filename for info in archive.infolist()}

                    required = cls.OFFICE_REQUIRED_MEMBERS[extension]

                    missing = required - member_names

                    if missing:
                        raise ValidationError(
                            (
                                "Office file is malformed "
                                "or does not match its "
                                "extension."
                            )
                        )

                    cls._validate_office_active_content(archive)

            except ValidationError:
                raise

            except zipfile.BadZipFile as exc:
                raise ValidationError(
                    ("Office document archive " "is malformed.")
                ) from exc

            except Exception as exc:
                raise ValidationError(
                    ("Office document could not " "be safely inspected.")
                ) from exc

        finally:
            uploaded_file.seek(current_position)

    @classmethod
    def _validate_zip_members(
        cls,
        archive,
    ):
        infos = archive.infolist()

        max_entries = getattr(
            settings,
            "DOCUMENT_UPLOAD_MAX_ARCHIVE_ENTRIES",
            5000,
        )

        if len(infos) > max_entries:
            raise ValidationError(
                ("Office document contains too " "many archive entries.")
            )

        max_uncompressed = getattr(
            settings,
            ("DOCUMENT_UPLOAD_MAX_ARCHIVE_" "UNCOMPRESSED_BYTES"),
            200 * 1024 * 1024,
        )

        max_ratio = getattr(
            settings,
            ("DOCUMENT_UPLOAD_MAX_ARCHIVE_" "COMPRESSION_RATIO"),
            200,
        )

        total_uncompressed = 0

        for info in infos:
            cls._validate_zip_member_path(info.filename)

            if info.flag_bits & 0x1:
                raise ValidationError(
                    ("Encrypted Office archive " "entries are not allowed.")
                )

            total_uncompressed += info.file_size

            if total_uncompressed > max_uncompressed:
                raise ValidationError(
                    ("Office document expands " "beyond the safe processing " "limit.")
                )

            compressed_size = max(
                info.compress_size,
                1,
            )

            ratio = info.file_size / compressed_size

            if info.file_size >= 1024 * 1024 and ratio > max_ratio:
                raise ValidationError(
                    ("Office document has a " "suspicious compression ratio.")
                )

    @staticmethod
    def _validate_zip_member_path(
        name,
    ):
        normalized = name.replace(
            "\\",
            "/",
        )

        path = PurePosixPath(normalized)

        if path.is_absolute() or ".." in path.parts:
            raise ValidationError(("Office document contains " "unsafe archive paths."))

    @classmethod
    def _validate_office_active_content(
        cls,
        archive,
    ):
        lower_names = [info.filename.lower() for info in archive.infolist()]

        for name in lower_names:
            if any(forbidden in name for forbidden in cls.OFFICE_FORBIDDEN_PARTS):
                raise ValidationError(
                    (
                        "Office document contains "
                        "macros or embedded active "
                        "content."
                    )
                )

        content_types_info = None

        for info in archive.infolist():
            if info.filename == "[Content_Types].xml":
                content_types_info = info
                break

        if not content_types_info:
            raise ValidationError(
                ("Office document is missing " "content-type metadata.")
            )

        if content_types_info.file_size > 1024 * 1024:
            raise ValidationError(
                ("Office content-type metadata " "is unexpectedly large.")
            )

        content_types = archive.read(content_types_info).lower()

        if any(
            marker in content_types for marker in cls.OFFICE_FORBIDDEN_CONTENT_MARKERS
        ):
            raise ValidationError(("Macro-enabled Office files " "are not allowed."))

    @classmethod
    def _validate_image(
        cls,
        uploaded_file,
    ):
        current_position = uploaded_file.tell()

        try:
            uploaded_file.seek(0)

            try:
                with warnings.catch_warnings():
                    warnings.simplefilter(
                        "error",
                        Image.DecompressionBombWarning,
                    )

                    image = Image.open(uploaded_file)

                    width, height = image.size

                    max_pixels = getattr(
                        settings,
                        ("DOCUMENT_UPLOAD_" "MAX_IMAGE_PIXELS"),
                        50_000_000,
                    )

                    if width <= 0 or height <= 0:
                        raise ValidationError(("Image dimensions " "are invalid."))

                    if width * height > max_pixels:
                        raise ValidationError(
                            ("Image dimensions " "exceed the safe " "processing limit.")
                        )

                    image.verify()

            except ValidationError:
                raise

            except (
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ) as exc:
                raise ValidationError(
                    ("Image is too large to " "process safely.")
                ) from exc

            except Exception as exc:
                raise ValidationError(("Image is malformed or " "unreadable.")) from exc

        finally:
            uploaded_file.seek(current_position)

    @classmethod
    def _validate_json_file(cls, uploaded_file):
        current_position = uploaded_file.tell()

        def unique_object(pairs):
            result = dict(pairs)
            if len(result) != len(pairs):
                raise ValueError("Duplicate JSON fields")
            return result

        try:
            uploaded_file.seek(0)
            raw = uploaded_file.read(8388609)
            if len(raw) > 8388608:
                raise ValidationError("JSON documents must not exceed 8 MiB.")
            value = json.loads(
                raw.decode("utf-8-sig"),
                object_pairs_hook=unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(value, (dict, list)):
                raise ValueError("A JSON document must contain an object or array")
        except (ValueError, UnicodeError, RecursionError):
            raise ValidationError("Malformed JSON document.")
        finally:
            uploaded_file.seek(current_position)

    @classmethod
    def _validate_text_file(
        cls,
        uploaded_file,
    ):
        current_position = uploaded_file.tell()

        try:
            uploaded_file.seek(0)

            sample = uploaded_file.read(cls.READ_SAMPLE_BYTES)

            if not sample:
                raise ValidationError("Empty text files are not allowed.")

            decoded = None

            for encoding in (
                "utf-8-sig",
                "utf-16",
            ):
                try:
                    decoded = sample.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue

            if decoded is None:
                raise ValidationError(
                    ("Text/CSV files must use " "UTF-8 or UTF-16 encoding.")
                )

            if "\x00" in decoded:
                raise ValidationError(
                    ("Text file contains binary " "data and was rejected.")
                )

            if decoded:
                suspicious_controls = sum(
                    1
                    for char in decoded
                    if (
                        ord(char) < 32
                        and char
                        not in {
                            "\n",
                            "\r",
                            "\t",
                        }
                    )
                )

                ratio = suspicious_controls / len(decoded)

                if ratio > 0.01:
                    raise ValidationError(
                        (
                            "Text file contains "
                            "unexpected binary "
                            "control characters."
                        )
                    )

        finally:
            uploaded_file.seek(current_position)

    @staticmethod
    def _rewind(
        uploaded_file,
    ):
        if hasattr(
            uploaded_file,
            "seek",
        ):
            uploaded_file.seek(0)

    @staticmethod
    def calculate_checksum(
        uploaded_file,
    ):
        sha256 = hashlib.sha256()

        current_position = uploaded_file.tell()

        uploaded_file.seek(0)

        for chunk in uploaded_file.chunks():
            sha256.update(chunk)

        uploaded_file.seek(current_position)

        return sha256.hexdigest()
