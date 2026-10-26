import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import ValidationError

from apps.workflows.services.workflow_definition_service import bounded_json

SEVERITIES = ("info", "low", "medium", "high", "critical")
CONTEXT = {
    "contract_type",
    "jurisdiction",
    "governing_law",
    "currency",
    "contract_value",
    "data_processing",
}
DECISIONS = {
    "confirmed",
    "dismissed",
    "accepted",
    "negotiation_required",
    "exception_approved",
    "needs_more_information",
}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value):
        raise ValidationError("Invalid clause rule or fact identifier.")
    return value


def strings(value, limit=64, size=150):
    if (
        not isinstance(value, list)
        or len(value) > limit
        or any(not isinstance(v, str) or not 1 <= len(v) <= size for v in value)
        or len(set(value)) != len(value)
    ):
        raise ValidationError("Invalid bounded string list.")
    return value


def schema(value):
    bounded_json(value, 8192)
    if not isinstance(value, dict) or len(value) > 32:
        raise ValidationError("Invalid clause fact schema.")
    for key, spec in value.items():
        identifier(key)
        if (
            not isinstance(spec, dict)
            or set(spec) - {"type", "values"}
            or not isinstance(spec.get("type"), str)
            or spec.get("type") not in {"text", "decimal", "boolean", "date", "enum"}
        ):
            raise ValidationError("Unsupported clause fact type.")
        if spec["type"] == "enum":
            if not strings(spec.get("values"), 32, 100):
                raise ValidationError("An enum requires allowed values.")
        elif "values" in spec:
            raise ValidationError("Only enum facts accept values.")
    return value


def scalar(value, spec):
    if value is None:
        return None
    kind = spec["type"]
    if kind == "boolean":
        if type(value) is not bool:
            raise ValidationError("Clause boolean facts require true or false.")
        return value
    if not isinstance(value, str) or not 1 <= len(value) <= 2000:
        raise ValidationError("Clause facts require bounded strings.")
    if kind == "decimal":
        if not re.fullmatch(r"-?\d{1,24}(?:\.\d{1,12})?", value):
            raise ValidationError("Clause decimal facts require exact decimal strings.")
        try:
            if not Decimal(value).is_finite():
                raise InvalidOperation
        except InvalidOperation:
            raise ValidationError("Invalid clause decimal fact.")
    elif kind == "date":
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise ValidationError("Clause dates require an unambiguous ISO date.")
    elif kind == "enum" and value not in spec["values"]:
        raise ValidationError("Unsupported normalized clause fact.")
    return value


def facts(value, fact_schema):
    bounded_json(value, 32768)
    if not isinstance(value, dict) or set(value) - set(fact_schema):
        raise ValidationError("Clause facts do not match the published schema.")
    for key, fact in value.items():
        if not isinstance(fact, dict) or set(fact) != {
            "value",
            "raw_value",
            "evidence",
        }:
            raise ValidationError("Each clause fact requires a value and source proof.")
        scalar(fact["value"], fact_schema[key])
        if (
            not isinstance(fact["raw_value"], str)
            or not 1 <= len(fact["raw_value"]) <= 4000
            or not isinstance(fact["evidence"], list)
            or not 1 <= len(fact["evidence"]) <= 20
        ):
            raise ValidationError("Invalid clause fact provenance.")
        # Semantic boolean interpretation is explicitly reviewed, never verified by confidence.
        if (
            fact_schema[key]["type"] in {"decimal", "date", "enum", "text"}
            and fact["value"] is not None
            and fact["value"] not in fact["raw_value"]
        ):
            raise ValidationError(
                "A normalized fact must be supported by its raw value."
            )
    return value


def predicate(value, allowed):
    if (
        not isinstance(value, dict)
        or set(value) != {"field", "operator", "value"}
        or not isinstance(value["field"], str)
        or value["field"] not in allowed
    ):
        raise ValidationError("Invalid structured playbook predicate.")
    kind = allowed[value["field"]]["type"]
    if not isinstance(value["operator"], str) or value["operator"] not in (
        {"eq", "ne", "gte", "lte"}
        if kind in {"decimal", "date"}
        else {"eq", "ne", "contains"}
        if kind == "text"
        else {"eq", "ne"}
    ):
        raise ValidationError("Unsupported typed comparison operator.")
    scalar(value["value"], allowed[value["field"]])
    if value["value"] is None:
        raise ValidationError("Playbook comparisons require an explicit value.")
    return value


def predicates(value, allowed):
    if not isinstance(value, list) or len(value) > 16:
        raise ValidationError("Playbook conditions must be bounded predicates.")
    return [predicate(p, allowed) for p in value]


def disjoint(left, right):
    return any(
        a["field"] == b["field"]
        and a["operator"] == b["operator"] == "eq"
        and (
            Decimal(a["value"]) != Decimal(b["value"])
            if a["field"] == "contract_value"
            else a["value"] != b["value"]
        )
        for a in left
        for b in right
    )


def rules(value, taxonomy):
    bounded_json(value, 65536)
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ValidationError("A playbook needs 1 to 64 rules.")
    result, codes = [], set()
    context_schema = {
        k: {
            "type": "decimal"
            if k == "contract_value"
            else "boolean"
            if k == "data_processing"
            else "text"
        }
        for k in CONTEXT
    }
    for raw in value:
        if not isinstance(raw, dict) or set(raw) - {
            "code",
            "clause_type",
            "requirement",
            "when",
            "preferred",
            "fallback",
            "prohibited",
            "severity",
            "requires_approval",
        }:
            raise ValidationError("Unsupported playbook rule properties.")
        code = identifier(raw.get("code"))
        if (
            code in codes
            or not isinstance(raw.get("clause_type"), str)
            or raw.get("clause_type") not in taxonomy
            or not isinstance(raw.get("requirement"), str)
            or raw.get("requirement") not in {"required", "optional", "prohibited"}
        ):
            raise ValidationError("Invalid or duplicate playbook rule identity.")
        codes.add(code)
        rule = {
            "code": code,
            "clause_type": raw["clause_type"],
            "requirement": raw["requirement"],
            "severity": raw.get("severity", "medium"),
            "requires_approval": raw.get("requires_approval", False),
        }
        if (
            rule["severity"] not in SEVERITIES
            or type(rule["requires_approval"]) is not bool
        ):
            raise ValidationError("Invalid playbook severity or approval policy.")
        rule["when"] = predicates(raw.get("when", []), context_schema)
        spec = taxonomy[rule["clause_type"]]["fact_schema"]
        for key in ("preferred", "fallback", "prohibited"):
            rule[key] = predicates(raw.get(key, []), spec)
        if (
            rule["fallback"]
            and not rule["preferred"]
            or rule["preferred"]
            and rule["preferred"] == rule["prohibited"]
            or rule["fallback"]
            and rule["fallback"] == rule["prohibited"]
        ):
            raise ValidationError(
                "Conflicting preferred, fallback or prohibited positions."
            )
        if any(
            old["clause_type"] == rule["clause_type"]
            and not disjoint(old["when"], rule["when"])
            for old in result
        ):
            raise ValidationError(
                "Overlapping clause requirements must be combined into one rule or have disjoint conditions."
            )
        result.append(rule)
    return result


def evaluate(conditions, values, fact_schema=None):
    unknown = False
    for test in conditions:
        observed = values.get(test["field"])
        if observed is None:
            unknown = True
            continue
        expected, op = test["value"], test["operator"]
        if (fact_schema and fact_schema[test["field"]]["type"] == "decimal") or test[
            "field"
        ] == "contract_value":
            observed, expected = Decimal(observed), Decimal(expected)
        if op == "contains":
            matches = (
                isinstance(observed, str) and expected.casefold() in observed.casefold()
            )
        else:
            matches = {
                "eq": lambda: observed == expected,
                "ne": lambda: observed != expected,
                "gte": lambda: observed >= expected,
                "lte": lambda: observed <= expected,
            }[op]()
        if not matches:
            return False
    return None if unknown else True


def comparison(rule, observed, spec, *, present, complete):
    applies = evaluate(rule["when"], observed.get("context", {}))
    if applies is False:
        return "not_applicable"
    if applies is None:
        return "ambiguous"
    if not present:
        return (
            "coverage_insufficient"
            if not complete
            else "not_detected"
            if rule["requirement"] == "required"
            else "not_applicable"
        )
    if rule["requirement"] == "prohibited":
        return "prohibited"
    values = observed.get("facts", {})
    if rule["prohibited"]:
        prohibited = evaluate(rule["prohibited"], values, spec)
        if prohibited is None:
            return "ambiguous"
        if prohibited is True:
            return "prohibited"
    if not rule["preferred"]:
        return "matching"
    preferred = evaluate(rule["preferred"], values, spec)
    if preferred is True:
        return "preferred"
    if rule["fallback"]:
        fallback = evaluate(rule["fallback"], values, spec)
        if fallback is True:
            return "acceptable_fallback"
        if fallback is None:
            return "ambiguous"
    return "ambiguous" if preferred is None else "deviation"


def json_answer(answer):
    if not isinstance(answer, str) or len(answer.encode()) > 65536:
        raise ValidationError("Invalid bounded clause model response.")
    try:
        value = json.loads(answer)
    except (ValueError, RecursionError):
        raise ValidationError("Clause model response must be structured JSON.")
    return bounded_json(value, 65536)
