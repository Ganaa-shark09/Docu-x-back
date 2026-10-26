import csv
import re
from pathlib import Path

from docx import Document as DocxDocument
from openpyxl import load_workbook

from apps.ai_engine.models import AIConversationDocument
from apps.documents.models import Document


class ExternalDocumentExactAnswerService:
    """Deterministic answers for structurally exact document questions.

    LLMs are intentionally not used for one-based positional lookups such as
    "the 14th record" because counting inside long context is error-prone.
    This service only handles requests that can be resolved exactly from
    document structure; everything else falls through to normal semantic QA.
    """

    TARGET_PATTERN = (
        r"record|records|item|items|entry|entries|row|rows|"
        r"bullet(?:\s+point)?|bullet(?:\s+points)?|point|points"
    )

    WORD_ORDINALS = {
        "first": 1,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "fifth": 5,
        "sixth": 6,
        "seventh": 7,
        "eighth": 8,
        "ninth": 9,
        "tenth": 10,
        "eleventh": 11,
        "twelfth": 12,
        "thirteenth": 13,
        "fourteenth": 14,
        "fifteenth": 15,
        "sixteenth": 16,
        "seventeenth": 17,
        "eighteenth": 18,
        "nineteenth": 19,
        "twentieth": 20,
    }

    @classmethod
    def try_answer(
        cls,
        user,
        conversation,
        query,
        explicit_document_uuids=None,
    ):
        request = cls.parse_positional_request(query)
        if not request:
            return None

        documents = cls.get_candidate_documents(
            user=user,
            conversation=conversation,
            explicit_document_uuids=explicit_document_uuids,
        )

        # Exact positional lookup is only safe when the target document is
        # unambiguous. If several documents are attached, let semantic QA ask
        # or infer which document is intended instead of returning a value from
        # the wrong file.
        if len(documents) != 1:
            return None

        document = documents[0]
        units = cls.extract_structural_units(document, target=request["target"])
        position = request["position"]

        if not units or position < 1 or position > len(units):
            return None

        selected = units[position - 1].strip()
        if not selected:
            return None

        ordinal_label = cls.ordinal_label(position)
        target_label = cls.singular_target(request["target"])

        return {
            "answer": f"The {ordinal_label} {target_label} in the document is:\n\n{selected}",
            "sources": [
                {
                    "source_type": "external_document",
                    "document_uuid": str(document.uuid),
                    "governance_revision": getattr(
                        document, "governance_revision", None
                    ),
                    "document_title": document.title,
                    "chunk_index": position - 1,
                    "score": 1.0,
                    "content_preview": selected[:1600],
                }
            ],
            "metadata": {
                "exact_structural_answer": True,
                "document_uuid": str(document.uuid),
                "document_title": document.title,
                "position": position,
                "target": target_label,
                "unit_count": len(units),
                "indexing": "one_based",
            },
        }

    @classmethod
    def parse_positional_request(cls, query):
        normalized = (query or "").lower().strip()
        if not normalized:
            return None

        numeric_before_target = re.search(
            rf"\b(?P<number>\d{{1,4}})(?:st|nd|rd|th)?\s+"
            rf"(?P<target>{cls.TARGET_PATTERN})\b",
            normalized,
        )
        if numeric_before_target:
            return {
                "position": int(numeric_before_target.group("number")),
                "target": numeric_before_target.group("target"),
            }

        target_before_numeric = re.search(
            rf"\b(?P<target>{cls.TARGET_PATTERN})\s*"
            rf"(?:number\s*|no\.?\s*|#\s*)?(?P<number>\d{{1,4}})\b",
            normalized,
        )
        if target_before_numeric:
            return {
                "position": int(target_before_numeric.group("number")),
                "target": target_before_numeric.group("target"),
            }

        ordinal_words = "|".join(cls.WORD_ORDINALS)
        word_before_target = re.search(
            rf"\b(?P<ordinal>{ordinal_words})\s+"
            rf"(?P<target>{cls.TARGET_PATTERN})\b",
            normalized,
        )
        if word_before_target:
            return {
                "position": cls.WORD_ORDINALS[word_before_target.group("ordinal")],
                "target": word_before_target.group("target"),
            }

        return None

    @classmethod
    def get_candidate_documents(cls, user, conversation, explicit_document_uuids=None):
        from apps.documents.services import DocumentPermissionService

        if (
            conversation.company_id != user.company_id
            or conversation.user_id != user.pk
            or conversation.scope != "external"
            or conversation.is_deleted
        ):
            return []
        queryset = Document.objects.filter(
            company=user.company,
            uploaded_by=user,
            scope=Document.Scope.EXTERNAL,
            is_deleted=False,
            is_ai_ready=True,
        ).exclude(status=Document.Status.FAILED)

        permitted = DocumentPermissionService.get_accessible_document_queryset(
            user, scope=Document.Scope.EXTERNAL, ai_policy_only=True
        ).values("pk")
        queryset = queryset.filter(pk__in=permitted)

        if explicit_document_uuids:
            return list(
                queryset.filter(
                    uuid__in=explicit_document_uuids,
                    ai_conversation_attachments__conversation=conversation,
                    ai_conversation_attachments__is_deleted=False,
                )
                .distinct()
                .order_by("created_at")
            )

        attached_document_ids = AIConversationDocument.objects.filter(
            company=user.company,
            conversation=conversation,
            is_deleted=False,
            document__scope=Document.Scope.EXTERNAL,
            document__is_deleted=False,
        ).values_list("document_id", flat=True)

        return list(
            queryset.filter(id__in=attached_document_ids).order_by("created_at")
        )

    @classmethod
    def extract_structural_units(cls, document, target):
        extension = Path(document.original_filename or "").suffix.lower()
        file_path = cls.safe_file_path(document)

        if extension == ".docx" and file_path:
            units = cls.extract_docx_units(file_path=file_path, target=target)
            if units:
                return units

        if extension == ".xlsx" and file_path:
            units = cls.extract_xlsx_units(file_path=file_path)
            if units:
                return units

        if extension == ".csv" and file_path:
            units = cls.extract_csv_units(file_path=file_path)
            if units:
                return units

        extraction = getattr(document, "text_extraction", None)
        text = extraction.extracted_text if extraction else ""
        return cls.extract_units_from_text(text)

    @staticmethod
    def safe_file_path(document):
        try:
            return document.file.path
        except (AttributeError, NotImplementedError, ValueError):
            return ""

    @classmethod
    def extract_docx_units(cls, file_path, target):
        doc = DocxDocument(file_path)

        if cls.singular_target(target) == "row":
            rows = []
            for table in doc.tables:
                for row in table.rows:
                    values = [cell.text.strip() for cell in row.cells]
                    if any(values):
                        rows.append(" | ".join(values))
            if rows:
                return rows

        # Use the largest contiguous list block. This avoids mixing a main
        # record list with a later "Summary" bullet list and preserves true
        # one-based ordering from the source DOCX.
        segments = []
        current = []

        for paragraph in doc.paragraphs:
            text = paragraph.text.strip()
            style_name = (paragraph.style.name or "").lower()
            is_list = bool(text) and any(
                marker in style_name for marker in ("list", "bullet", "number")
            )

            if is_list:
                current.append(text)
                continue

            if current:
                segments.append(current)
                current = []

        if current:
            segments.append(current)

        if segments:
            return max(segments, key=len)

        return []

    @staticmethod
    def extract_xlsx_units(file_path):
        workbook = load_workbook(file_path, read_only=True, data_only=True)
        rows = []

        try:
            for sheet in workbook.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    values = [str(value).strip() for value in row if value is not None]
                    if values:
                        rows.append(" | ".join(values))
        finally:
            workbook.close()

        return rows

    @staticmethod
    def extract_csv_units(file_path):
        rows = []
        with open(
            file_path, "r", encoding="utf-8", errors="ignore", newline=""
        ) as handle:
            reader = csv.reader(handle)
            for row in reader:
                values = [str(value).strip() for value in row if str(value).strip()]
                if values:
                    rows.append(" | ".join(values))
        return rows

    @classmethod
    def extract_units_from_text(cls, text):
        lines = [line.strip() for line in (text or "").splitlines()]
        segments = []
        current = []

        for line in lines:
            if not line:
                if current:
                    segments.append(current)
                    current = []
                continue

            if cls.looks_like_structural_line(line):
                current.append(cls.strip_list_marker(line))
            elif current:
                segments.append(current)
                current = []

        if current:
            segments.append(current)

        if not segments:
            return []

        return max(segments, key=len)

    @staticmethod
    def looks_like_structural_line(line):
        if re.match(r"^[\-\*•●▪◦]\s+", line):
            return True
        if re.match(r"^\d{1,4}[\.)]\s+", line):
            return True
        if line.count("|") >= 2:
            return True
        if line.count("\t") >= 2:
            return True
        return False

    @staticmethod
    def strip_list_marker(line):
        line = re.sub(r"^[\-\*•●▪◦]\s+", "", line)
        line = re.sub(r"^\d{1,4}[\.)]\s+", "", line)
        return line.strip()

    @staticmethod
    def singular_target(target):
        normalized = (target or "record").lower()
        if normalized.startswith("bullet"):
            return "bullet point"
        mapping = {
            "records": "record",
            "items": "item",
            "entries": "entry",
            "rows": "row",
            "points": "point",
        }
        return mapping.get(normalized, normalized)

    @staticmethod
    def ordinal_label(number):
        if 10 <= number % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
        return f"{number}{suffix}"
