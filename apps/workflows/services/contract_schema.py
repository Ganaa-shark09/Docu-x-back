import json
import re
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import ValidationError

from apps.workflows.services.invoice_schema import CURRENCIES, normalized_date, number

TEXT_FIELDS = (
    "contract_title",
    "contract_type",
    "term_type",
    "initial_term",
    "renewal_term",
    "renewal_mechanism",
    "termination_notice_period",
    "termination_for_convenience",
    "termination_for_cause",
    "cure_period",
    "termination_fees",
    "survival_obligations",
    "governing_law",
    "jurisdiction",
    "venue",
    "dispute_resolution",
    "payment_terms",
    "payment_frequency",
    "pricing",
    "late_fees",
    "credits",
    "renewal_pricing",
    "price_increase",
    "liability_cap",
)
DATE_FIELDS = (
    "effective_date",
    "execution_date",
    "start_date",
    "expiration_date",
    "renewal_date",
    "notice_deadline",
)
MONEY_FIELDS = ("contract_value", "minimum_commitment", "maximum_commitment")
CLAUSES = (
    "confidentiality",
    "termination",
    "limitation_of_liability",
    "indemnification",
    "payment",
    "renewal",
    "governing_law",
    "data_protection",
    "intellectual_property",
    "warranty",
    "insurance",
    "audit_rights",
    "assignment",
    "force_majeure",
    "notice",
    "service_levels",
    "non_solicitation",
    "non_compete",
)
PARTY_ROLES = (
    "customer",
    "vendor",
    "supplier",
    "licensor",
    "licensee",
    "employer",
    "employee",
    "landlord",
    "tenant",
    "disclosing_party",
    "receiving_party",
    "other",
    "unknown",
)
ROW_FIELDS = {
    "parties": (
        "legal_name",
        "role",
        "address",
        "jurisdiction",
        "registration_identifier",
        "signatory",
    ),
    "key_contacts": ("name", "role", "email"),
    "obligations": (
        "party",
        "action",
        "timing",
        "timing_type",
        "due_date",
        "condition",
        "frequency",
    ),
    "key_clauses": ("category", "text", "section_heading"),
    "schedule_dates": ("kind", "raw_date", "date", "description"),
}
FIELDS = (
    *TEXT_FIELDS,
    *DATE_FIELDS,
    "currency",
    *MONEY_FIELDS,
    "auto_renewal",
    *ROW_FIELDS,
)
TYPE_PATTERNS = {
    "nda": ["non-disclosure agreement", "nondisclosure agreement", "nda"],
    "msa": ["master services agreement", "master service agreement", "msa"],
    "service_agreement": ["services agreement", "service agreement"],
    "sow": ["statement of work", "sow"],
    "vendor_agreement": ["vendor agreement"],
    "customer_agreement": ["customer agreement"],
    "employment_agreement": ["employment agreement"],
    "licence_agreement": ["license agreement", "licence agreement"],
    "lease": ["lease agreement", "lease"],
    "partnership_agreement": ["partnership agreement"],
}


def slug(value):
    return re.sub(r"[\s/-]+", "_", value.strip().lower())


def finding(
    code, fields=(), *, category="missing_information", severity="medium", blocking=True
):
    return {
        "code": code,
        "fields": list(fields),
        "category": category,
        "severity": severity,
        "blocking": blocking,
    }


class ContractPolicy:
    DEFAULTS = {
        "schema_version": 1,
        "required_fields": ["contract_title", "parties", "effective_date"],
        "required_clauses": ["termination", "governing_law"],
        "confidence_threshold": "0.85",
        "risk_review_threshold": "medium",
        "require_approval": True,
        "force_review": False,
        "use_ai": False,
        "date_order": None,
        "decimal_separator": None,
        "type_patterns": TYPE_PATTERNS,
        "risk_rules": [],
        "max_text_bytes": 2097152,
        "max_chunks": 256,
        "max_items": 100,
    }

    @classmethod
    def validate(cls, options):
        if not isinstance(options, dict) or set(options) - (
            set(cls.DEFAULTS) | {"clause_playbook_version"}
        ):
            raise ValidationError("Unsupported contract policy fields.")
        if "clause_playbook_version" in options:
            from apps.workflows.services.clause_definition_service import (
                options as clause_options,
            )

            clause_options({"playbook_version": options["clause_playbook_version"]})
        result = {**cls.DEFAULTS, **options}
        if type(result["schema_version"]) is not int or result["schema_version"] != 1:
            raise ValidationError("Unsupported contract schema version.")
        for key, allowed in [
            ("required_fields", FIELDS),
            ("required_clauses", CLAUSES),
        ]:
            values = result[key]
            if (
                not isinstance(values, list)
                or len(values) > len(allowed)
                or any(not isinstance(v, str) or v not in allowed for v in values)
                or len(set(values)) != len(values)
            ):
                raise ValidationError("Invalid contract policy requirements.")
        for key in ("require_approval", "force_review", "use_ai"):
            if type(result[key]) is not bool:
                raise ValidationError("Contract policy switches must be booleans.")
        try:
            value = result["confidence_threshold"]
            if (
                not isinstance(value, str)
                or not Decimal(value).is_finite()
                or not 0 <= Decimal(value) <= 1
            ):
                raise ValueError
        except (ValueError, InvalidOperation):
            raise ValidationError("Invalid contract confidence threshold.")
        if result["date_order"] not in (None, "DMY", "MDY") or result[
            "decimal_separator"
        ] not in (None, ".", ","):
            raise ValidationError("Invalid contract normalization policy.")
        if result["risk_review_threshold"] not in (
            "info",
            "low",
            "medium",
            "high",
            "critical",
        ):
            raise ValidationError("Invalid risk review threshold.")
        for key, low, high in [
            ("max_text_bytes", 1000, 2097152),
            ("max_chunks", 1, 256),
            ("max_items", 1, 100),
        ]:
            if type(result[key]) is not int or not low <= result[key] <= high:
                raise ValidationError("Invalid contract processing bound.")
        types = result["type_patterns"]
        if not isinstance(types, dict) or not 1 <= len(types) <= 50:
            raise ValidationError("Invalid contract type configuration.")
        for key, terms in types.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,59}", key)
                or not isinstance(terms, list)
                or not 1 <= len(terms) <= 10
                or any(not isinstance(t, str) or not 1 <= len(t) <= 120 for t in terms)
            ):
                raise ValidationError("Invalid contract type patterns.")
        rules = result["risk_rules"]
        if not isinstance(rules, list) or len(rules) > 30:
            raise ValidationError("Invalid contract risk rules.")
        for rule in rules:
            if not isinstance(rule, dict) or set(rule) != {
                "code",
                "field",
                "operator",
                "value",
                "category",
                "severity",
            }:
                raise ValidationError("Invalid contract risk rule schema.")
            if (
                rule["field"] not in (*TEXT_FIELDS, *MONEY_FIELDS, "auto_renewal")
                or rule["operator"] not in ("eq", "contains", "gte", "lte")
                or rule["category"]
                not in (
                    "commercial",
                    "legal",
                    "operational",
                    "privacy",
                    "security",
                    "financial",
                    "renewal",
                    "termination",
                    "liability",
                    "compliance",
                )
                or rule["severity"] not in ("info", "low", "medium", "high", "critical")
                or not isinstance(rule["code"], str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,59}", rule["code"])
            ):
                raise ValidationError("Invalid contract risk rule.")
            if rule["field"] in MONEY_FIELDS or rule["operator"] in ("gte", "lte"):
                try:
                    if (
                        rule["field"] not in MONEY_FIELDS
                        or rule["operator"] not in ("eq", "gte", "lte")
                        or not isinstance(rule["value"], str)
                        or not Decimal(rule["value"]).is_finite()
                    ):
                        raise ValueError
                except (InvalidOperation, ValueError):
                    raise ValidationError(
                        "Numeric contract rules require Decimal strings."
                    )
            elif rule["field"] == "auto_renewal":
                if rule["operator"] != "eq" or type(rule["value"]) is not bool:
                    raise ValidationError("Invalid renewal rule.")
            elif (
                not isinstance(rule["value"], str) or not 1 <= len(rule["value"]) <= 500
            ):
                raise ValidationError("Invalid contract rule value.")
        return result


def normalize(values, policy):
    data = dict.fromkeys(FIELDS)
    errors = []
    for key, raw in values.items():
        if key not in data or raw is None or raw == "":
            continue
        try:
            if key in ROW_FIELDS:
                rows = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(rows, list) or len(rows) > policy["max_items"]:
                    raise ValueError("invalid_structure")
                normalized = []
                for row in rows:
                    if (
                        not isinstance(row, dict)
                        or set(row) - set((*ROW_FIELDS[key], "evidence"))
                        or any(
                            v is not None and (not isinstance(v, str) or len(v) > 4000)
                            for k, v in row.items()
                            if k != "evidence"
                        )
                    ):
                        raise ValueError("invalid_structure")
                    item = {k: row.get(k) for k in ROW_FIELDS[key]}
                    item["evidence"] = row.get("evidence", [])
                    if (
                        not isinstance(item["evidence"], list)
                        or len(item["evidence"]) > 20
                    ):
                        raise ValueError("invalid_evidence")
                    required = {
                        "parties": "legal_name",
                        "obligations": "action",
                        "key_clauses": "text",
                        "key_contacts": "name",
                        "schedule_dates": "raw_date",
                    }[key]
                    if not item[required]:
                        raise ValueError("invalid_structure")
                    if key == "parties":
                        item["role"] = slug(item["role"] or "unknown")
                        if item["role"] not in PARTY_ROLES:
                            raise ValueError("invalid_party_role")
                    if key == "key_clauses":
                        item["category"] = slug(item["category"] or "")
                        if item["category"] not in CLAUSES:
                            raise ValueError("invalid_clause_category")
                    if key == "obligations":
                        if item["timing_type"] not in (
                            None,
                            "fixed_date",
                            "relative",
                            "recurring",
                            "before_renewal",
                            "unknown",
                        ):
                            raise ValueError("invalid_timing")
                        if item["due_date"]:
                            item["due_date"] = normalized_date(
                                item["due_date"], policy["date_order"]
                            )
                    if key == "schedule_dates":
                        if item["kind"] not in ("payment", "milestone"):
                            raise ValueError("invalid_schedule_kind")
                        item["date"] = normalized_date(
                            item["raw_date"], policy["date_order"]
                        )
                    normalized.append(item)
                data[key] = normalized
            elif not isinstance(raw, str) or len(raw) > 4000:
                raise ValueError("invalid_text")
            elif key in DATE_FIELDS:
                data[key] = normalized_date(raw, policy["date_order"])
            elif key in MONEY_FIELDS:
                if values.get("currency") is not None and not isinstance(
                    values["currency"], str
                ):
                    raise ValueError("invalid_currency")
                data[key] = number(
                    raw,
                    policy["decimal_separator"],
                    currency=(values.get("currency") or "").upper() or None,
                )
            elif key == "currency":
                code = raw.strip().upper()
                if code not in CURRENCIES:
                    raise ValueError("invalid_currency")
                data[key] = code
            elif key == "auto_renewal":
                if raw.strip().lower() not in (
                    "true",
                    "false",
                    "yes",
                    "no",
                    "automatic",
                    "manual",
                ):
                    raise ValueError("ambiguous_renewal")
                data[key] = raw.strip().lower() in ("true", "yes", "automatic")
            elif key == "term_type":
                value = slug(raw)
                if value not in (
                    "fixed_term",
                    "evergreen",
                    "automatic_renewal",
                    "manual_renewal",
                    "indefinite",
                    "unknown",
                ):
                    raise ValueError("ambiguous_term")
                data[key] = value
            elif key == "contract_type":
                match = next(
                    (
                        k
                        for k, terms in policy["type_patterns"].items()
                        if raw.lower() == k or raw.lower() in terms
                    ),
                    None,
                )
                data[key] = match or "unknown"
            else:
                data[key] = raw.strip()
        except (ValueError, InvalidOperation, TypeError, RecursionError) as exc:
            code = (
                str(exc)
                if str(exc)
                in {
                    "ambiguous_date",
                    "ambiguous_number",
                    "ambiguous_term",
                    "ambiguous_renewal",
                }
                else "invalid_field"
            )
            errors.append(finding(code, [key], category="ambiguous_term"))
    data["contract_type"] = data["contract_type"] or "unknown"
    return data, errors


def assess(data, policy):
    errors = [
        finding("required_field_missing", [k])
        for k in policy["required_fields"]
        if data.get(k) in (None, "", [])
    ]
    detected = {c["category"] for c in data.get("key_clauses") or []}
    errors += [
        finding("missing_clause_" + k, ["key_clauses"], category="missing_clause")
        for k in policy["required_clauses"]
        if k not in detected
    ]
    if data["contract_type"] == "unknown":
        errors.append(
            finding(
                "contract_classification_uncertain",
                ["contract_type"],
                category="ambiguous_term",
            )
        )
    parties = data.get("parties") or []
    if (
        len(parties) < 2
        or len({p["legal_name"].casefold() for p in parties}) != len(parties)
        or any(p["role"] == "unknown" for p in parties)
    ):
        errors.append(
            finding("party_ambiguous", ["parties"], category="ambiguous_term")
        )
    if (
        data.get("expiration_date")
        and data.get("effective_date")
        and data["expiration_date"] < data["effective_date"]
    ):
        errors.append(
            finding(
                "conflicting_dates",
                ["expiration_date", "effective_date"],
                category="evidence_conflict",
            )
        )
    if data.get("auto_renewal") is True and not data.get("termination_notice_period"):
        errors.append(
            finding(
                "renewal_notice_unknown",
                ["auto_renewal", "termination_notice_period"],
                category="renewal_warning",
            )
        )
    levels = ["info", "low", "medium", "high", "critical"]
    for rule in policy["risk_rules"]:
        actual, expected = data.get(rule["field"]), rule["value"]
        if actual is None:
            continue
        op = rule["operator"]
        matched = (
            (
                Decimal(actual) == Decimal(expected)
                if rule["field"] in MONEY_FIELDS
                else actual == expected
            )
            if op == "eq"
            else str(expected).casefold() in str(actual).casefold()
            if op == "contains"
            else Decimal(actual) >= Decimal(expected)
            if op == "gte"
            else Decimal(actual) <= Decimal(expected)
        )
        if matched:
            item = finding(
                rule["code"],
                [rule["field"]],
                category="risk",
                severity=rule["severity"],
                blocking=levels.index(rule["severity"])
                >= levels.index(policy["risk_review_threshold"]),
            )
            item["risk_category"] = rule["category"]
            errors.append(item)
    return errors
