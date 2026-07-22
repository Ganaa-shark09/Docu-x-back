import re

from apps.data_firewall.models import FirewallScan


class FirewallScanService:
    PATTERNS = {
        "email": re.compile(r"[\w\.-]+@[\w\.-]+\.\w+"),
        "phone_number": re.compile(r"(\+?\d[\d\s\-]{8,}\d)"),
        "credit_card_candidate": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
        "api_key_candidate": re.compile(
            r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token|bearer)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"
        ),
        "password_candidate": re.compile(
            r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"]?[^\s'\"]{6,}"
        ),
        "pan_candidate": re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"),
        "aadhaar_candidate": re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),
        "iban_candidate": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    }

    @classmethod
    def scan_document(cls, document):
        extraction = getattr(document, "text_extraction", None)
        text = extraction.extracted_text if extraction else ""

        detected_patterns = cls.detect_patterns(text)
        redacted_text = cls.redact_text(text)

        risk_level, decision = cls._calculate_risk(detected_patterns)

        scan, _ = FirewallScan.objects.update_or_create(
            document=document,
            defaults={
                "risk_level": risk_level,
                "decision": decision,
                "detected_patterns": detected_patterns,
                "redacted_preview": redacted_text[:5000],
                "scan_summary": cls._build_summary(
                    detected_patterns, risk_level, decision
                ),
                "can_use_cloud_llm": decision
                not in [
                    FirewallScan.Decision.REQUIRE_APPROVAL,
                    FirewallScan.Decision.BLOCK_CLOUD_LLM,
                ],
                "requires_human_approval": decision
                in [
                    FirewallScan.Decision.REQUIRE_APPROVAL,
                    FirewallScan.Decision.BLOCK_CLOUD_LLM,
                ],
            },
        )

        return scan

    @classmethod
    def detect_patterns(cls, text):
        detected_patterns = []

        for pattern_name, regex in cls.PATTERNS.items():
            matches = regex.findall(text or "")

            if matches:
                detected_patterns.append(
                    {
                        "pattern": pattern_name,
                        "count": len(matches),
                    }
                )

        return detected_patterns

    @classmethod
    def redact_text(cls, text):
        redacted_text = text or ""

        for pattern_name, regex in cls.PATTERNS.items():
            redacted_text = regex.sub(
                f"[REDACTED_{pattern_name.upper()}]",
                redacted_text,
            )

        return redacted_text

    @classmethod
    def _calculate_risk(cls, detected_patterns):
        pattern_names = {item["pattern"] for item in detected_patterns}

        if (
            "api_key_candidate" in pattern_names
            or "password_candidate" in pattern_names
        ):
            return (
                FirewallScan.RiskLevel.CRITICAL,
                FirewallScan.Decision.BLOCK_CLOUD_LLM,
            )

        if (
            "credit_card_candidate" in pattern_names
            or "aadhaar_candidate" in pattern_names
        ):
            return FirewallScan.RiskLevel.HIGH, FirewallScan.Decision.REQUIRE_APPROVAL

        if "pan_candidate" in pattern_names or "iban_candidate" in pattern_names:
            return FirewallScan.RiskLevel.MEDIUM, FirewallScan.Decision.REDACT_AND_ALLOW

        if detected_patterns:
            return FirewallScan.RiskLevel.MEDIUM, FirewallScan.Decision.REDACT_AND_ALLOW

        return FirewallScan.RiskLevel.LOW, FirewallScan.Decision.ALLOW

    @staticmethod
    def _build_summary(detected_patterns, risk_level, decision):
        if not detected_patterns:
            return "No sensitive patterns detected."

        names = ", ".join(item["pattern"] for item in detected_patterns)

        return (
            f"Detected sensitive patterns: {names}. "
            f"Risk level: {risk_level}. Decision: {decision}."
        )
