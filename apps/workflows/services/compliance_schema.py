"""Bounded, declarative compliance configuration. No expressions or standards text."""

import re
from datetime import date
from uuid import UUID

from rest_framework.exceptions import ValidationError
from apps.workflows.services.workflow_definition_service import bounded_json

TESTS = {
    "missing_evidence",
    "expired_evidence",
    "missing_approval",
    "overdue_review",
    "unmapped_control",
}
OUTCOMES = {
    "satisfied",
    "partially_satisfied",
    "not_satisfied",
    "insufficient_evidence",
    "not_applicable",
    "review_required",
}
APPLICABILITY = {
    "applicable",
    "not_applicable",
    "conditionally_applicable",
    "undetermined",
}
SOURCE_KINDS = {
    "document",
    "hr_policy_version",
    "contract_output",
    "clause_output",
    "finance_output",
    "rfp_output",
    "workflow_artifact",
    "invoice_output",
}
DIMENSIONS = {"business_units", "locations", "systems"}


def obj(value, keys, required=()):
    if (
        not isinstance(value, dict)
        or set(value) - set(keys)
        or set(required) - set(value)
    ):
        raise ValidationError("Unsupported or missing structured configuration fields.")
    bounded_json(value)
    return value


def text(value, maximum=200, blank=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not blank and not value.strip())
    ):
        raise ValidationError("A bounded nonempty text value is required.")
    return value


def code(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value
    ):
        raise ValidationError("Invalid stable code.")
    return value


def choice(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValidationError("Unsupported state or configuration choice.")
    return value


def iso(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValidationError("An explicit ISO date is required.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError("Invalid calendar date.")


def uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("A valid source UUID is required.")


def strings(values, limit=100, uuids=False):
    if not isinstance(values, list) or len(values) > limit:
        raise ValidationError("A bounded list is required.")
    result = [uuid(v) if uuids else text(v, 100) for v in values]
    if len(set(result)) != len(result):
        raise ValidationError("Duplicate scope values are unsupported.")
    return result


def scope(value):
    obj(
        value,
        DIMENSIONS | {"start", "end", "document_uuids", "control_uuids"},
        {"start", "end", "document_uuids"},
    )
    if iso(value["start"]) > iso(value["end"]):
        raise ValidationError("Assessment period is reversed.")
    result = {
        "start": value["start"],
        "end": value["end"],
        "document_uuids": strings(value["document_uuids"], 20, True),
    }
    if not result["document_uuids"]:
        raise ValidationError("Select at least one explicit source document.")
    for key in DIMENSIONS:
        result[key] = strings(value.get(key, []))
    result["control_uuids"] = strings(value.get("control_uuids", []), 500, True)
    return result


def applicability(value):
    obj(value, {"dimension", "values"})
    if value:
        choice(value.get("dimension"), DIMENSIONS)
        values = strings(value.get("values"))
        if not values:
            raise ValidationError("Conditional applicability needs explicit values.")
    return value


def framework(value):
    obj(value, {"requirements", "policy"}, {"requirements"})
    requirements = value["requirements"]
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 500:
        raise ValidationError("A framework needs 1 to 500 structured requirements.")
    result = []
    codes = set()
    for row in requirements:
        obj(
            row,
            {
                "code",
                "title",
                "category",
                "type",
                "obligation",
                "parent",
                "applicability",
                "source",
            },
            {"code", "title"},
        )
        key = code(row["code"])
        if key in codes:
            raise ValidationError("Requirement codes must be unique.")
        codes.add(key)
        source = obj(row.get("source", {}), {"reference", "location", "description"})
        for content in source.values():
            text(content, 1000)
        result.append(
            {
                "code": key,
                "title": text(row["title"]),
                "category": text(row.get("category", "general"), 80),
                "type": choice(
                    row.get("type", "control"),
                    {"control", "policy", "process", "reporting", "record"},
                ),
                "obligation": choice(
                    row.get("obligation", "mandatory"),
                    {"mandatory", "optional", "conditional"},
                ),
                "parent": code(row["parent"]) if row.get("parent") else None,
                "applicability": applicability(row.get("applicability", {})),
                "source": source,
            }
        )
    parents = {row["code"]: row["parent"] for row in result}
    for key in parents:
        seen = set()
        while key is not None:
            if key not in parents or key in seen or len(seen) >= 8:
                raise ValidationError(
                    "Requirement hierarchy is invalid or exceeds eight levels."
                )
            seen.add(key)
            key = parents[key]
    policy = obj(
        value.get("policy", {}),
        {"severity", "allow_risk_acceptance", "require_na_approval"},
    )
    severity = choice(
        policy.get("severity", "high"), {"info", "low", "medium", "high", "critical"}
    )
    for key in {"allow_risk_acceptance", "require_na_approval"}:
        if key in policy and type(policy[key]) is not bool:
            raise ValidationError("Policy switches must be booleans.")
    return {
        "requirements": result,
        "policy": {
            "severity": severity,
            "allow_risk_acceptance": policy.get("allow_risk_acceptance", False),
            "require_na_approval": policy.get("require_na_approval", True),
        },
    }


def control(value):
    obj(
        value,
        {
            "description",
            "objective",
            "owner",
            "type",
            "frequency",
            "evidence_expectations",
            "tests",
            "effective_from",
            "effective_until",
            "status",
        },
        {"objective", "owner"},
    )
    tests = strings(value.get("tests", sorted(TESTS)), 5)
    if not tests or set(tests) - TESTS:
        raise ValidationError("Only protected declarative tests are supported.")
    result = {
        "description": text(value.get("description", ""), 2000, True),
        "objective": text(value["objective"], 2000),
        "owner": value["owner"],
        "type": choice(
            value.get("type", "preventive"), {"preventive", "detective", "corrective"}
        ),
        "frequency": choice(
            value.get("frequency", "annual"),
            {
                "continuous",
                "daily",
                "weekly",
                "monthly",
                "quarterly",
                "annual",
                "on_event",
            },
        ),
        "evidence_expectations": strings(value.get("evidence_expectations", []), 20),
        "tests": tests,
        "status": choice(value.get("status", "active"), {"active", "draft", "retired"}),
    }
    for key in {"effective_from", "effective_until"}:
        if value.get(key):
            iso(value[key])
            result[key] = value[key]
    if (
        result.get("effective_from")
        and result.get("effective_until")
        and result["effective_from"] > result["effective_until"]
    ):
        raise ValidationError("Control dates are reversed.")
    return result


def dates(value):
    obj(value, {"issued", "effective", "expires", "review_due"})
    for content in value.values():
        iso(content)
    if (
        value.get("effective")
        and value.get("expires")
        and value["effective"] > value["expires"]
    ):
        raise ValidationError("Evidence dates are reversed.")
    return value
