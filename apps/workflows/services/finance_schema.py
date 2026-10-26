"""Provider-neutral, non-executable finance import and test configuration."""

import csv
import io
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import ValidationError

from apps.workflows.services.workflow_definition_service import bounded_json

MONEY = {
    "gross_amount",
    "net_amount",
    "tax_amount",
    "debit",
    "credit",
    "quantity",
    "unit_price",
}
DATES = {"transaction_date", "posting_date", "document_date", "receipt_date"}
TEXT = {
    "business_unit",
    "transaction_id",
    "external_reference",
    "transaction_type",
    "account_code",
    "account_name",
    "vendor_name",
    "vendor_identifier",
    "vendor_tax_id",
    "currency",
    "purchase_order_reference",
    "invoice_reference",
    "payment_reference",
    "cost_center",
    "department",
    "project_reference",
    "description",
    "status",
}
FIELDS = MONEY | DATES | TEXT | {"lines", "manual_entry"}
TESTS = (
    "integrity",
    "period",
    "cutoff",
    "amount",
    "duplicates",
    "support",
    "invoice",
    "vendor",
    "three_way",
    "journal",
    "frequency",
)
METHODS = {
    "random",
    "systematic",
    "high_value",
    "value_threshold",
    "risk_based",
    "stratified",
}


def money(value, nullable=True):
    if value is None or value == "":
        if nullable:
            return None
        raise ValidationError("A monetary value is required.")
    if (
        not isinstance(value, str)
        or len(value) > 40
        or not re.fullmatch(r"-?\d+(?:\.\d{1,8})?", value)
    ):
        raise ValidationError(
            "Money must be an exact decimal string, at most eight decimal places."
        )
    try:
        result = Decimal(value)
        if not result.is_finite() or result.copy_abs() >= Decimal("1e22"):
            raise InvalidOperation
        return result
    except InvalidOperation:
        raise ValidationError("Invalid monetary value.")


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValidationError("Dates must use YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError("Invalid date.")


def record(value):
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValidationError("Unknown financial record fields.")
    bounded_json(value, 65536)
    result = {}
    for key, raw in value.items():
        if key in MONEY:
            amount = money(raw)
            result[key] = str(amount) if amount is not None else None
        elif key in DATES:
            result[key] = iso_date(raw).isoformat() if raw not in (None, "") else None
        elif key in TEXT:
            if not isinstance(raw, str) or len(raw) > (
                2000 if key == "description" else 150
            ):
                raise ValidationError("Invalid financial text field.")
            result[key] = raw
        elif key == "manual_entry":
            if type(raw) is not bool:
                raise ValidationError("Manual-entry flag must be boolean.")
            result[key] = raw
        elif key == "lines":
            if not isinstance(raw, list) or len(raw) > 100:
                raise ValidationError("At most 100 transaction lines are supported.")
            result[key] = []
            for line in raw:
                if not isinstance(line, dict) or set(line) - (
                    MONEY
                    | {
                        "account_code",
                        "description",
                        "cost_center",
                        "department",
                        "project_reference",
                    }
                ):
                    raise ValidationError("Invalid transaction line.")
                result[key].append(record_line(line))
    for key in ("transaction_id", "transaction_date", "currency", "transaction_type"):
        if not result.get(key):
            raise ValidationError("Required financial record field is missing.")
    if len(result["transaction_type"]) > 40:
        raise ValidationError("Transaction type exceeds forty characters.")
    if not re.fullmatch("[A-Z]{3}", result["currency"]):
        raise ValidationError(
            "Currency must be an explicit three-letter uppercase code."
        )
    if all(
        result.get(key) is None for key in ("gross_amount", "debit", "credit")
    ) and not result.get("lines"):
        raise ValidationError("An amount or debit/credit lines are required.")
    return result


def record_line(value):
    result = {}
    for key, raw in value.items():
        if key in MONEY:
            amount = money(raw)
            result[key] = str(amount) if amount is not None else None
        elif not isinstance(raw, str) or len(raw) > 2000:
            raise ValidationError("Invalid line text.")
        else:
            result[key] = raw
    return result


def parse(raw, kind, mapping):
    if len(raw) > 8388608 or kind not in {"csv", "json"}:
        raise ValidationError("Only CSV/JSON datasets up to 8 MiB are supported.")
    if (
        not isinstance(mapping, dict)
        or len(mapping) > 60
        or any(
            not isinstance(k, str)
            or len(k) > 150
            or not isinstance(v, str)
            or v not in FIELDS
            for k, v in mapping.items()
        )
        or len(set(mapping.values())) != len(mapping)
    ):
        raise ValidationError("Invalid schema mapping.")
    try:
        text = raw.decode("utf-8-sig")
        if kind == "csv":
            reader = csv.DictReader(io.StringIO(text), strict=True)
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(
                reader.fieldnames
            ):
                raise ValueError
            rows = reader
        else:

            def unique_object(pairs):
                result = dict(pairs)
                if len(result) != len(pairs):
                    raise ValueError("Duplicate JSON source fields")
                return result

            rows = json.loads(
                text,
                object_pairs_hook=unique_object,
                parse_float=lambda _: (_ for _ in ()).throw(ValueError()),
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(rows, list):
                raise ValueError
        count = 0
        for index, row in enumerate(rows, 1):
            if index > 10000 or not isinstance(row, dict) or None in row:
                raise ValueError
            names = mapping or {key: key for key in row}
            if any(key not in row for key in names):
                raise ValueError
            yield (
                index,
                record({target: row[source] for source, target in names.items()}),
                names,
            )
            count += 1
        if count == 0:
            raise ValueError
    except (UnicodeError, ValueError, TypeError, csv.Error, RecursionError):
        raise ValidationError(
            "Malformed financial dataset or unsupported numeric encoding."
        )


def scope(value):
    keys = {
        "start",
        "end",
        "period",
        "fiscal_year",
        "period_closed",
        "objectives",
        "transaction_types",
        "vendors",
        "accounts",
        "cost_centers",
        "business_units",
    }
    if (
        not isinstance(value, dict)
        or set(value) - keys
        or iso_date(value.get("start")) > iso_date(value.get("end"))
    ):
        raise ValidationError("Invalid audit scope or period.")
    if not isinstance(value.get("period", "custom"), str) or value.get(
        "period", "custom"
    ) not in {"monthly", "quarterly", "annual", "custom"}:
        raise ValidationError("Invalid period type.")
    if "period_closed" in value and type(value["period_closed"]) is not bool:
        raise ValidationError("Invalid period state.")
    for key in keys - {"start", "end", "period", "fiscal_year", "period_closed"}:
        if key in value and (
            not isinstance(value[key], list)
            or len(value[key]) > 100
            or any(not isinstance(x, str) or len(x) > 200 for x in value[key])
        ):
            raise ValidationError("Invalid scope dimension.")
    if "fiscal_year" in value and (
        not isinstance(value["fiscal_year"], str) or len(value["fiscal_year"]) > 30
    ):
        raise ValidationError("Fiscal year must be explicitly labelled.")
    bounded_json(value, 32768)
    return value


def policy(value):
    keys = {
        "tests",
        "materiality",
        "tolerance",
        "cutoff_days",
        "posting_days",
        "duplicate_days",
        "frequency_limit",
        "blocking_severities",
        "sampling",
        "required_support_types",
        "severities",
    }
    if not isinstance(value, dict) or set(value) - keys:
        raise ValidationError("Unknown audit policy fields.")
    value = {"tests": list(TESTS), "blocking_severities": ["high", "critical"], **value}
    if (
        not isinstance(value["tests"], list)
        or not value["tests"]
        or any(not isinstance(x, str) for x in value["tests"])
        or len(set(value["tests"])) != len(value["tests"])
        or set(value["tests"]) - set(TESTS)
    ):
        raise ValidationError("Unknown deterministic audit test.")
    if (
        not isinstance(value["blocking_severities"], list)
        or any(not isinstance(x, str) for x in value["blocking_severities"])
        or set(value["blocking_severities"])
        - {"info", "low", "medium", "high", "critical"}
    ):
        raise ValidationError("Invalid finding severity policy.")
    for key in ("materiality", "tolerance"):
        config = value.get(key, {})
        if not isinstance(config, dict) or len(config) > 100:
            raise ValidationError("Thresholds require explicit currency keys.")
        for currency, amount in config.items():
            if (
                not isinstance(currency, str)
                or not re.fullmatch("[A-Z]{3}", currency)
                or money(amount, False) < 0
            ):
                raise ValidationError("Invalid threshold.")
    for key in ("cutoff_days", "posting_days", "duplicate_days", "frequency_limit"):
        if key in value and (
            type(value[key]) is not int or not 0 <= value[key] <= 3660
        ):
            raise ValidationError("Invalid bounded audit rule parameter.")
    if "required_support_types" in value and (
        not isinstance(value["required_support_types"], list)
        or len(value["required_support_types"]) > 30
        or any(
            not isinstance(x, str) or len(x) > 40
            for x in value["required_support_types"]
        )
    ):
        raise ValidationError("Invalid support policy.")
    severities = value.get("severities", {})
    if (
        not isinstance(severities, dict)
        or set(severities) - set(TESTS)
        or any(
            not isinstance(x, str)
            or x not in {"info", "low", "medium", "high", "critical"}
            for x in severities.values()
        )
    ):
        raise ValidationError("Invalid test severity policy.")
    if "sampling" in value:
        sampling(value["sampling"])
    bounded_json(value, 32768)
    return value


def sampling(value):
    if (
        not isinstance(value, dict)
        or set(value) - {"method", "size", "seed", "thresholds"}
        or not isinstance(value.get("method"), str)
        or value.get("method") not in METHODS
        or type(value.get("size")) is not int
        or not 1 <= value["size"] <= 5000
    ):
        raise ValidationError("Invalid sampling configuration.")
    if value["method"] in {"random", "stratified"} and (
        not isinstance(value.get("seed"), str) or not 1 <= len(value["seed"]) <= 128
    ):
        raise ValidationError("A reproducible sampling seed is required.")
    thresholds = value.get("thresholds", {})
    if not isinstance(thresholds, dict) or len(thresholds) > 100:
        raise ValidationError("Invalid sampling thresholds.")
    for currency, amount in thresholds.items():
        if (
            not isinstance(currency, str)
            or not re.fullmatch("[A-Z]{3}", currency)
            or money(amount, False) < 0
        ):
            raise ValidationError("Invalid sample threshold.")
    return value
