"""Bounded solicitation extraction. Source text is data, never instructions."""

import hashlib
import re
from datetime import date, datetime

from rest_framework.exceptions import ValidationError

from apps.workflows.services.contract_extraction_service import evidence


METADATA = {
    "title": ("rfp title", "solicitation title", "project title"),
    "issuer": ("issuer", "issuing organization"),
    "reference": ("rfp number", "solicitation number", "reference number"),
    "contact": ("contact", "contact email"),
    "submission_method": ("submission method", "submission portal"),
    "format": ("response format", "file format"),
    "page_limit": ("page limit",),
    "word_limit": ("word limit",),
    "category": ("procurement category",),
    "term": ("contract term",),
    "value": ("estimated value",),
}
DEADLINES = {
    "issue_date": ("issue date", "issued on"),
    "response_deadline": ("response deadline", "submission deadline", "due date"),
    "question_deadline": ("question deadline", "questions due"),
    "intent_deadline": ("intent deadline", "intent to respond"),
}
MAX_TEXT_BYTES = 2097152
MAX_ITEMS = 2000


def normalized_deadline(raw):
    """Only ISO dates/timestamps are unambiguous without a locale policy."""
    value = {
        "raw": raw,
        "date": None,
        "datetime": None,
        "timezone": None,
        "review_required": True,
    }
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            value.update(
                date=date.fromisoformat(raw).isoformat(), review_required=False
            )
        elif re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?", raw
        ):
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            value.update(datetime=parsed.isoformat(), review_required=False)
            if parsed.tzinfo:
                value["timezone"] = raw[-6:] if not raw.endswith("Z") else "Z"
    except ValueError:
        pass
    return value


def extract(document, text, *, supporting_solicitation=False):
    if not isinstance(text, str) or len(text.encode()) > MAX_TEXT_BYTES:
        raise ValidationError("Solicitation source exceeds the extraction limit.")
    classified = bool(
        re.search(
            r"(?im)^\s*(?:#{1,6}\s*)?(?:request for proposals?\b|request for quotation\b|rfp\b|security questionnaire\b)",
            text,
        )
    )
    result = {
        "classified": classified,
        "metadata": {},
        "deadlines": [],
        "sections": [],
        "items": [],
        "submission_requirements": [],
        "review_required": True,
        "coverage": "structured_candidates",
    }
    if not classified and not supporting_solicitation:
        return result
    for target, aliases in METADATA.items():
        matches = list(
            re.finditer(
                r"(?im)^[ \t]*(?:"
                + "|".join(map(re.escape, aliases))
                + r")[ \t]*:[ \t]*([^\r\n]{1,500})$",
                text,
            )
        )
        if matches:
            result["metadata"][target] = [
                {
                    "value": m[1].strip(),
                    "evidence": evidence(document, text, m.start(1), m.end(1)),
                    "review_required": True,
                }
                for m in matches[:20]
            ]
    for target, aliases in DEADLINES.items():
        for m in list(
            re.finditer(
                r"(?im)^[ \t]*(?:"
                + "|".join(map(re.escape, aliases))
                + r")[ \t]*:[ \t]*([^\r\n]{1,200})$",
                text,
            )
        )[:20]:
            result["deadlines"].append(
                {
                    "kind": target,
                    **normalized_deadline(m[1].strip()),
                    "evidence": evidence(document, text, m.start(1), m.end(1)),
                }
            )
    section = "General"
    offset = 0
    for line in text.splitlines(keepends=True):
        raw = line.strip()
        heading = re.match(r"^(?:#{1,6}\s+|(?:SECTION|Section)\s+)(.+)$", raw)
        if heading:
            if len(result["sections"]) >= MAX_ITEMS:
                raise ValidationError(
                    "Solicitation has too many sections for one package."
                )
            section = heading[1][:200]
            result["sections"].append(
                {
                    "heading": section,
                    "evidence": evidence(
                        document, text, offset, offset + len(line.rstrip())
                    ),
                }
            )
        numbered = re.match(r"^(?:[A-Z]{0,5}[-.]?)?\d+(?:\.\d+)*[.)\s|]\s*(.+)$", raw)
        question = bool(
            numbered
            and (
                "?" in raw
                or re.search(
                    r"\b(?:describe|explain|provide|list|confirm)\b", raw, re.I
                )
            )
        )
        requirement = bool(
            re.search(r"\b(?:must|shall|required|should|optional)\b", raw, re.I)
        )
        if raw and (question or requirement):
            if len(raw) > 8000:
                raise ValidationError(
                    "A solicitation item exceeds the supported text limit."
                )
            if len(result["items"]) >= MAX_ITEMS:
                raise ValidationError(
                    "Solicitation requires a smaller extraction batch."
                )
            start = offset + line.index(raw)
            stable = hashlib.sha256(
                f"{document.uuid}:{start}:{raw}".encode()
            ).hexdigest()[:24]
            limits = [
                {
                    "value": int(m[1]),
                    "unit": "words" if m[2].lower().startswith("word") else "pages",
                    "evidence": evidence(
                        document, text, start + m.start(), start + m.end()
                    ),
                }
                for m in list(
                    re.finditer(
                        r"(?i)\b(?:max(?:imum)?\.?\s*|limit\s*[:=]?\s*|up to\s*|no more than\s*)(\d{1,6})\s*(words?|pages?)\b",
                        raw,
                    )
                )[:4]
            ]
            result["items"].append(
                {
                    "key": stable,
                    "section": section,
                    "kind": "question" if question else "requirement",
                    "text": raw,
                    "mandatory": bool(
                        re.search(r"\b(?:must|shall|required)\b", raw, re.I)
                    ),
                    "response_required": question or requirement,
                    "limits": limits,
                    "source_number": raw[: numbered.start(1)].strip()
                    if numbered
                    else None,
                    "table_cells": [c.strip() for c in raw.split("|")]
                    if "|" in raw
                    else [],
                    "evidence": evidence(document, text, start, start + len(raw)),
                    "review_required": True,
                }
            )
        if raw and re.search(
            r"\b(?:attach|attachment|submit|signature|page limit|word limit|file name|filename|copies|certificate)\b",
            raw,
            re.I,
        ):
            if len(result["submission_requirements"]) >= MAX_ITEMS or len(raw) > 8000:
                raise ValidationError(
                    "Solicitation submission requirements exceed the package limits."
                )
            start = offset + line.index(raw)
            result["submission_requirements"].append(
                {
                    "text": raw,
                    "mandatory": bool(
                        re.search(r"\b(?:must|shall|required)\b", raw, re.I)
                    ),
                    "attachment": bool(
                        re.search(r"\b(?:attach|attachment|certificate)\b", raw, re.I)
                    ),
                    "evidence": evidence(document, text, start, start + len(raw)),
                    "review_required": True,
                }
            )
        offset += len(line)
    return result
