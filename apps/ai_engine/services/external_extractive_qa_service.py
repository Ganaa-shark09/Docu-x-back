import re

from apps.ai_engine.models import AIConversationDocument
from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService


class ExternalExtractiveQAService:
    EXTRACTION_KEYWORDS = {
        "what is",
        "what was",
        "show me",
        "extract",
        "read",
        "copy",
        "give me",
        "tell me",
    }

    TARGET_KEYWORDS = {
        "prompt",
        "message",
        "instruction",
        "instructions",
        "turn",
        "user turn",
        "assistant turn",
        "agent turn",
        "text",
        "content",
    }

    @classmethod
    def try_answer(
        cls,
        user,
        conversation,
        query,
        explicit_document_uuids=None,
    ):
        if not cls.should_use_extractive_answer(query):
            return None

        documents = cls.get_candidate_documents(
            user=user,
            conversation=conversation,
            explicit_document_uuids=explicit_document_uuids,
        )

        for document in documents:
            extracted_text = cls.get_extracted_text(document)

            if not extracted_text:
                continue

            answer_text = cls.extract_answer_from_text(
                query=query,
                text=extracted_text,
            )

            if answer_text:
                return {
                    "answer": cls.format_answer(
                        query=query,
                        answer_text=answer_text,
                        document=document,
                    ),
                    "sources": [
                        {
                            "source_type": "external_document",
                            "document_uuid": str(document.uuid),
                            "governance_revision": getattr(
                                document, "governance_revision", None
                            ),
                            "document_title": document.title,
                            "chunk_index": 0,
                            "score": 1.0,
                            "content_preview": answer_text[:1600],
                        }
                    ],
                    "metadata": {
                        "extractive": True,
                        "document_uuid": str(document.uuid),
                        "document_title": document.title,
                    },
                }

        return None

    @classmethod
    def should_use_extractive_answer(cls, query):
        normalized = query.lower()

        has_extraction_word = any(
            keyword in normalized for keyword in cls.EXTRACTION_KEYWORDS
        )
        has_target_word = any(keyword in normalized for keyword in cls.TARGET_KEYWORDS)

        return has_extraction_word and has_target_word

    @classmethod
    def get_candidate_documents(cls, user, conversation, explicit_document_uuids=None):
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
        ).exclude(
            status=Document.Status.FAILED,
        )
        permitted = DocumentPermissionService.get_accessible_document_queryset(
            user, scope=Document.Scope.EXTERNAL, require_ai=True
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

    @staticmethod
    def get_extracted_text(document):
        extraction = getattr(document, "text_extraction", None)

        if not extraction:
            return ""

        return extraction.extracted_text or ""

    @classmethod
    def extract_answer_from_text(cls, query, text):
        normalized_query = query.lower()
        normalized_text = cls.normalize_text(text)

        speaker = cls.extract_speaker(normalized_query)
        turn_number = cls.extract_turn_number(normalized_query)

        if turn_number:
            answer = cls.extract_turn_section(
                text=normalized_text,
                turn_number=turn_number,
                speaker=speaker,
            )

            if answer:
                return answer

        if speaker == "user":
            answer = cls.extract_user_instruction_fallback(normalized_text)
            if answer:
                return answer

        if speaker in {"assistant", "agent"}:
            answer = cls.extract_assistant_instruction_fallback(normalized_text)
            if answer:
                return answer

        if "prompt" in normalized_query or "instruction" in normalized_query:
            answer = cls.extract_user_instruction_fallback(normalized_text)
            if answer:
                return answer

        return None

    @staticmethod
    def normalize_text(text):
        text = text or ""

        replacements = {
            "\r\n": "\n",
            "\r": "\n",
            "User User turn": "User turn",
            "Assistant Assistant": "Assistant",
            "Agent Agent": "Agent",
            "Tums": "Turns",
            "tums": "turns",
            "WGPL": "WGPU",
            "renc": "render",
            "pase": "phase",
            "cli ": "cli ",
        }

        for old, new in replacements.items():
            text = text.replace(old, new)

        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text.strip()

    @staticmethod
    def extract_turn_number(query):
        match = re.search(r"\bturn\s*(\d+)\b", query)

        if not match:
            return None

        return int(match.group(1))

    @staticmethod
    def extract_speaker(query):
        if "user" in query:
            return "user"

        if "assistant" in query:
            return "assistant"

        if "agent" in query:
            return "agent"

        return None

    @classmethod
    def extract_turn_section(cls, text, turn_number, speaker=None):
        marker_patterns = cls.get_marker_patterns(turn_number, speaker)

        for marker_pattern in marker_patterns:
            match = re.search(marker_pattern, text, flags=re.I)

            if not match:
                continue

            section_start = match.end()
            section = cls.slice_section(
                text=text,
                start=section_start,
                speaker=speaker,
            )

            cleaned = cls.clean_extracted_section(section)

            if cleaned:
                return cleaned

        # OCR fallback: marker missing but query asks for user turn 1.
        if speaker == "user" and turn_number == 1:
            return cls.extract_user_instruction_fallback(text)

        if speaker in {"assistant", "agent"} and turn_number == 1:
            return cls.extract_assistant_instruction_fallback(text)

        return None

    @staticmethod
    def get_marker_patterns(turn_number, speaker=None):
        if speaker == "user":
            return [
                rf"\bUser\s+turn\s*{turn_number}\b",
                rf"\bUser\s+Turn\s*{turn_number}\b",
                rf"\bUser\s*-\s*turn\s*{turn_number}\b",
            ]

        if speaker == "assistant":
            return [
                rf"\bAssistant\s+turn\s*{turn_number}\b",
                rf"\bAssistant\s+Turn\s*{turn_number}\b",
                rf"\bAssistant\s*-\s*turn\s*{turn_number}\b",
            ]

        if speaker == "agent":
            return [
                rf"\bAgent\s+turn\s*{turn_number}\b",
                rf"\bAgent\s+Turn\s*{turn_number}\b",
                rf"\bAgent\s*-\s*turn\s*{turn_number}\b",
            ]

        return [
            rf"\bUser\s+turn\s*{turn_number}\b",
            rf"\bAssistant\s+turn\s*{turn_number}\b",
            rf"\bAgent\s+turn\s*{turn_number}\b",
            rf"\bturn\s*{turn_number}\b",
        ]

    @classmethod
    def slice_section(cls, text, start, speaker=None):
        if speaker == "user":
            stop_patterns = [
                r"\bAssistant\b",
                r"\bAssistant\s+Turn\s+\d+\b",
                r"\bAgent\s+Thinking\b",
                r"\bAgent\s+turn\s+\d+\b",
                r"\btodowrite\b",
                r"\bbash\b",
                r"\bcompleted\b",
                r"\bI'll\b",
                r"\bI will\b",
            ]
        elif speaker in {"assistant", "agent"}:
            stop_patterns = [
                r"\bUser\s+turn\s+\d+\b",
                r"\btodowrite\b",
                r"\bbash\b",
                r"\bcompleted\b",
            ]
        else:
            stop_patterns = [
                r"\bUser\s+turn\s+\d+\b",
                r"\bAssistant\s+turn\s+\d+\b",
                r"\bAgent\s+turn\s+\d+\b",
                r"\btodowrite\b",
                r"\bbash\b",
                r"\bcompleted\b",
            ]

        stop_positions = []

        for pattern in stop_patterns:
            match = re.search(pattern, text[start:], flags=re.I)
            if match and start + match.start() > start + 40:
                stop_positions.append(start + match.start())

        end = min(stop_positions) if stop_positions else min(len(text), start + 6000)

        return text[start:end]

    @classmethod
    def extract_user_instruction_fallback(cls, text):
        # Generic fallback for screenshots where OCR loses the "User turn 1" marker.
        start_patterns = [
            r"\bLet's\s+create\b",
            r"\bLet['’]s\s+create\b",
            r"\bCreate\s+a\b",
            r"\bPlease\s+create\b",
            r"\bBuild\s+a\b",
            r"\bWe\s+want\s+to\b",
        ]

        starts = []

        for pattern in start_patterns:
            match = re.search(pattern, text, flags=re.I)
            if match:
                starts.append(match.start())

        if not starts:
            return ""

        start = min(starts)

        stop_patterns = [
            r"\bAssistant\b",
            r"\bAgent\s+Thinking\b",
            r"\bAgent\s+turn\s+\d+\b",
            r"\btodowrite\b",
            r"\bbash\b",
            r"\bI'll\b",
            r"\bI will\b",
        ]

        stop_positions = []

        for pattern in stop_patterns:
            match = re.search(pattern, text[start:], flags=re.I)
            if match and start + match.start() > start + 80:
                stop_positions.append(start + match.start())

        end = min(stop_positions) if stop_positions else min(len(text), start + 6000)

        return cls.clean_extracted_section(text[start:end])

    @classmethod
    def extract_assistant_instruction_fallback(cls, text):
        patterns = [
            r"\bI'll\b.*",
            r"\bI will\b.*",
            r"\bI can\b.*",
            r"\bI’ll\b.*",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I | re.S)
            if not match:
                continue

            section = match.group(0)
            section = cls.slice_section(section, 0, speaker="assistant")
            cleaned = cls.clean_extracted_section(section)

            if cleaned:
                return cleaned

        return ""

    @staticmethod
    def clean_extracted_section(section):
        section = section or ""

        section = re.sub(r"\bUser\s+turn\s*\d+\b", "", section, flags=re.I)
        section = re.sub(r"\bAssistant\s+turn\s*\d+\b", "", section, flags=re.I)
        section = re.sub(r"\bAgent\s+turn\s*\d+\b", "", section, flags=re.I)

        section = re.sub(r"^\s*[:\-–|]+\s*", "", section)
        section = re.sub(r"\s+", " ", section)

        section = section.strip(" :-|\n\t")

        if len(section) < 20:
            return ""

        return section[:5000]

    @staticmethod
    def format_answer(query, answer_text, document):
        normalized = query.lower()

        if "prompt" in normalized:
            label = "prompt"
        elif "instruction" in normalized:
            label = "instruction"
        elif "message" in normalized:
            label = "message"
        else:
            label = "text"

        return (
            f"The extracted {label} from the uploaded document is:\n\n" f"{answer_text}"
        )
