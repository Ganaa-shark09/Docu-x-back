import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext

from rest_framework.exceptions import ValidationError


TEXT_FIELDS = (
    "invoice_number",
    "vendor_name",
    "vendor_identifier",
    "vendor_tax_id",
    "vendor_address",
    "bill_to_name",
    "bill_to_address",
    "purchase_order_number",
    "payment_terms",
    "payment_method",
    "invoice_reference",
    "customer_reference",
)
DATE_FIELDS = ("invoice_date", "due_date")
MONEY_FIELDS = (
    "subtotal",
    "tax_total",
    "discount_total",
    "shipping_total",
    "other_charges",
    "grand_total",
    "amount_due",
)
LINE_TEXT = ("description", "sku", "unit", "purchase_order_line_reference")
LINE_NUMBERS = (
    "quantity",
    "unit_price",
    "discount",
    "tax_rate",
    "tax_amount",
    "line_subtotal",
    "line_total",
)
FIELDS = (*TEXT_FIELDS, *DATE_FIELDS, "currency", *MONEY_FIELDS, "line_items")
# ISO currency codes, rather than guesses from ambiguous '$' or invoice locale.
CURRENCIES = set(
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND "
    "BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLF CLP CNY COP CRC CUP CVE CZK "
    "DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD "
    "HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW "
    "KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU "
    "MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR "
    "PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD "
    "SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU "
    "UYW UZS VED VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWG".split()
)
ZERO_DECIMAL = set(
    "BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF".split()
)
THREE_DECIMAL = set("BHD IQD JOD KWD LYD OMR TND".split())


def precision(currency):
    return (
        0
        if currency in ZERO_DECIMAL
        else 3
        if currency in THREE_DECIMAL
        else 4
        if currency in {"CLF", "UYW"}
        else 2
    )


def number(raw, separator=None, *, currency=None, monetary=True):
    """Strict decimal parsing. Ambiguous separators require an explicit policy."""
    if not isinstance(raw, str) or len(raw) > 80:
        raise ValueError("invalid_number")
    value = raw.strip().replace("\u00a0", " ")
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1].strip()
    codes = []
    prefix = re.match(r"^([A-Za-z]{3})\s*", value)
    if prefix:
        codes.append(prefix[1].upper())
        value = value[prefix.end() :]
    suffix = re.search(r"\s*([A-Za-z]{3})$", value)
    if suffix:
        codes.append(suffix[1].upper())
        value = value[: suffix.start()]
    symbol = value[0] if value and value[0] in "$\u20ac\u00a3\u20b9" else None
    if codes or symbol:
        if not monetary:
            raise ValueError("invalid_number")
        if (
            any(
                code not in CURRENCIES or (currency and code != currency)
                for code in codes
            )
            or len(set(codes)) > 1
        ):
            raise ValueError("invalid_currency_amount")
        symbol_currency = {"\u20ac": "EUR", "\u00a3": "GBP", "\u20b9": "INR"}.get(
            symbol
        )
        if currency and symbol_currency and currency != symbol_currency:
            raise ValueError("invalid_currency_amount")
        if symbol:
            value = value[1:].strip()
    if negative:
        value = "-" + value
    if not re.fullmatch(r"[-+]?\d[\d., ']*", value):
        raise ValueError("invalid_number")
    sign = "-" if value.startswith("-") else ""
    value = value.lstrip("+-")
    if separator is None:
        if "." in value and "," in value:
            separator = "." if value.rfind(".") > value.rfind(",") else ","
        elif "." in value or "," in value:
            symbol = "." if "." in value else ","
            if value.count(symbol) != 1 or len(value.rsplit(symbol, 1)[1]) == 3:
                raise ValueError("ambiguous_number")
            separator = symbol
    parts = value.split(separator) if separator else [value]
    if len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
        raise ValueError("invalid_number")
    integral = parts[0]
    if re.search(r"[.,'][.,']", integral):
        raise ValueError("invalid_grouping")
    groups = re.split(r"[., ']+", integral)
    if len(groups) > 1 and not (
        (len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:]))
        or (
            len(groups[0]) <= 2
            and len(groups[-1]) == 3
            and all(len(g) == 2 for g in groups[1:-1])
        )
    ):
        raise ValueError("invalid_grouping")
    if not all(g.isdigit() for g in groups):
        raise ValueError("invalid_number")
    result = Decimal(
        sign + "".join(groups) + ("." + parts[1] if len(parts) == 2 else "")
    )
    if not result.is_finite() or result.copy_abs() > Decimal("999999999999999999"):
        raise ValueError("invalid_number")
    return format(result, "f")


def normalized_date(raw, order=None):
    value = raw.strip()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", value)
    if match:
        a, b, year = map(int, match.groups())
        if a == b:
            return date(year, a, b).isoformat()
        if order is None:
            order = "DMY" if a > 12 else "MDY" if b > 12 else None
        if order is None:
            raise ValueError("ambiguous_date")
        return date(
            year, b if order == "DMY" else a, a if order == "DMY" else b
        ).isoformat()
    for pattern in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError("invalid_date")


def issue(code, fields, severity="high", blocking=True):
    return {"code": code, "fields": fields, "severity": severity, "blocking": blocking}


class InvoicePolicy:
    DEFAULTS = {
        "required_fields": [
            "invoice_number",
            "invoice_date",
            "vendor_name",
            "currency",
            "subtotal",
            "grand_total",
        ],
        "confidence_threshold": "0.85",
        "tolerance": None,
        "decimal_separator": None,
        "date_order": None,
        "allow_due_before_invoice": False,
        "require_vendor": False,
        "require_po": False,
        "require_approval": True,
        "review_duplicates": True,
        "force_review": False,
        "use_ai": False,
        "po_adapter": None,
        "schema_version": 1,
    }

    @classmethod
    def validate(cls, options):
        if not isinstance(options, dict) or set(options) - set(cls.DEFAULTS):
            raise ValidationError("Unsupported invoice policy fields.")
        result = {**cls.DEFAULTS, **options}
        if type(result["schema_version"]) is not int or result["schema_version"] != 1:
            raise ValidationError("Unsupported invoice schema version.")
        if (
            not isinstance(result["required_fields"], list)
            or not result["required_fields"]
            or any(
                not isinstance(x, str) or x not in FIELDS
                for x in result["required_fields"]
            )
        ):
            raise ValidationError("Invalid required invoice fields.")
        for key in ("confidence_threshold", "tolerance"):
            if result[key] is None and key == "tolerance":
                continue
            try:
                if not isinstance(result[key], str):
                    raise ValueError
                value = Decimal(result[key])
                if not value.is_finite() or value < 0 or value > 1:
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise ValidationError(
                    "Invoice thresholds must be bounded decimal strings."
                )
        if result["decimal_separator"] not in (None, ".", ",") or result[
            "date_order"
        ] not in (None, "DMY", "MDY"):
            raise ValidationError("Unsupported invoice number/date convention.")
        for key, default in cls.DEFAULTS.items():
            if type(default) is bool and type(result[key]) is not bool:
                raise ValidationError("Invoice policy flags must be booleans.")
        if result["po_adapter"] is not None and (
            not isinstance(result["po_adapter"], str) or len(result["po_adapter"]) > 80
        ):
            raise ValidationError("Invalid purchase order adapter name.")
        return result


def normalize(values, policy):
    output = dict.fromkeys(FIELDS)
    errors = []
    for key in FIELDS:
        raw = values.get(key)
        if raw is None or raw == "":
            continue
        try:
            if key in TEXT_FIELDS:
                if not isinstance(raw, str) or len(raw) > 4000:
                    raise ValueError("invalid_text")
                output[key] = raw.strip() or None
            elif key in DATE_FIELDS:
                output[key] = normalized_date(raw, policy["date_order"])
            elif key == "currency":
                code = raw.strip().upper()
                if code not in CURRENCIES:
                    raise ValueError("invalid_currency")
                output[key] = code
            elif key in MONEY_FIELDS:
                output[key] = number(
                    raw, policy["decimal_separator"], currency=output["currency"]
                )
            else:
                if not isinstance(raw, list) or len(raw) > 200:
                    raise ValueError("invalid_line_items")
                lines = []
                for i, row in enumerate(raw):
                    if not isinstance(row, dict) or set(row) - set(
                        (*LINE_TEXT, *LINE_NUMBERS)
                    ):
                        raise ValueError("invalid_line_items")
                    item = dict.fromkeys((*LINE_TEXT, *LINE_NUMBERS))
                    for field, value in row.items():
                        if value is None or value == "":
                            continue
                        if not isinstance(value, str) or len(value) > 4000:
                            raise ValueError("invalid_line_items")
                        try:
                            numeric = (
                                value.strip().removesuffix("%").strip()
                                if field == "tax_rate"
                                else value
                            )
                            item[field] = (
                                number(
                                    numeric,
                                    policy["decimal_separator"],
                                    currency=output["currency"],
                                    monetary=field not in {"quantity", "tax_rate"},
                                )
                                if field in LINE_NUMBERS
                                else value.strip()
                            )
                        except ValueError as exc:
                            errors.append(issue(str(exc), [f"line_items.{i}.{field}"]))
                    lines.append(item)
                output[key] = lines
        except (ValueError, TypeError, AttributeError) as exc:
            code = (
                str(exc)
                if str(exc)
                in {
                    "invalid_number",
                    "ambiguous_number",
                    "invalid_grouping",
                    "ambiguous_date",
                    "invalid_date",
                    "invalid_currency",
                    "invalid_currency_amount",
                    "invalid_text",
                    "invalid_line_items",
                }
                else "invalid_field"
            )
            errors.append(issue(code, [key]))
    return output, errors


def validate_fields(data, policy):
    with localcontext() as context:
        context.prec = 180
        return _validate_fields(data, policy)


def _validate_fields(data, policy):
    errors = [
        issue("missing_required_field", [key])
        for key in policy["required_fields"]
        if data.get(key) in (None, "", [])
    ]
    if (
        data["due_date"]
        and data["invoice_date"]
        and data["due_date"] < data["invoice_date"]
        and not policy["allow_due_before_invoice"]
    ):
        errors.append(issue("invalid_date", ["due_date", "invoice_date"]))
    if data["currency"]:
        scale = precision(data["currency"])
        unit = Decimal(1).scaleb(-scale)
        for key in MONEY_FIELDS:
            if (
                data[key] is not None
                and Decimal(data[key]).as_tuple().exponent < -scale
                and Decimal(data[key]) != Decimal(data[key]).quantize(unit)
            ):
                errors.append(issue("invalid_currency_precision", [key]))
    return errors


def validate_financials(data, policy):
    with localcontext() as context:
        context.prec = 180
        return _validate_financials(data, policy)


def _validate_financials(data, policy):
    errors = []
    tolerance = (
        Decimal(policy["tolerance"])
        if policy["tolerance"] is not None
        else Decimal(1).scaleb(-precision(data["currency"]))
    )

    def decimal(key, row=data):
        return Decimal(row[key]) if row.get(key) is not None else None

    subtotal, total = decimal("subtotal"), decimal("grand_total")
    if subtotal is not None and total is not None:
        components = sum(
            (
                decimal(k)
                for k in ("tax_total", "shipping_total", "other_charges")
                if decimal(k) is not None
            ),
            Decimal(0),
        )
        expected = subtotal + components - (decimal("discount_total") or Decimal(0))
        if abs(expected - total) > tolerance:
            errors.append(
                issue(
                    "totals_mismatch",
                    [
                        "subtotal",
                        "tax_total",
                        "discount_total",
                        "shipping_total",
                        "other_charges",
                        "grand_total",
                    ],
                )
            )
    if (
        total is not None
        and decimal("amount_due") is not None
        and abs(total - decimal("amount_due")) > tolerance
    ):
        errors.append(
            issue("amount_due_differs", ["grand_total", "amount_due"], "medium", False)
        )
    rows = data["line_items"] or []
    for i, row in enumerate(rows):
        quantity, price, line_sub = (
            decimal(k, row) for k in ("quantity", "unit_price", "line_subtotal")
        )
        if (
            quantity is not None
            and price is not None
            and line_sub is not None
            and abs(quantity * price - line_sub) > tolerance
        ):
            errors.append(issue("line_subtotal_mismatch", [f"line_items.{i}"]))
        line_total, tax, rate = (
            decimal(k, row) for k in ("line_total", "tax_amount", "tax_rate")
        )
        discount = decimal("discount", row) or Decimal(0)
        base = (
            line_sub
            if line_sub is not None
            else quantity * price
            if quantity is not None and price is not None
            else None
        )
        if (
            line_sub is not None
            and rate is not None
            and tax is not None
            and abs((line_sub - discount) * rate / Decimal(100) - tax) > tolerance
        ):
            errors.append(issue("tax_validation_failed", [f"line_items.{i}"]))
        if (
            base is not None
            and line_total is not None
            and abs(base - discount + (tax or Decimal(0)) - line_total) > tolerance
        ):
            errors.append(issue("line_total_mismatch", [f"line_items.{i}"]))
    if (
        rows
        and subtotal is not None
        and all(r["line_subtotal"] is not None for r in rows)
    ):
        if (
            abs(sum((decimal("line_subtotal", r) for r in rows), Decimal(0)) - subtotal)
            > tolerance
        ):
            errors.append(
                issue("line_items_subtotal_mismatch", ["line_items", "subtotal"])
            )
    if rows and total is not None and all(r["line_total"] is not None for r in rows):
        if all(r["tax_amount"] is not None for r in rows) or decimal("tax_total") in (
            None,
            Decimal(0),
        ):
            expected = sum((decimal("line_total", row) for row in rows), Decimal(0))
            expected += (decimal("shipping_total") or Decimal(0)) + (
                decimal("other_charges") or Decimal(0)
            )
            # Header discount_total includes discounts already present in the
            # line totals. Subtract only the remaining invoice-level portion.
            # Otherwise the same discount is deducted twice, incorrectly
            # reporting a mismatch for an internally consistent invoice.
            line_discounts = sum(
                (decimal("discount", row) or Decimal(0) for row in rows),
                Decimal(0),
            )
            header_discount = decimal("discount_total") or Decimal(0)
            remaining_discount = header_discount - line_discounts
            if remaining_discount < -tolerance:
                errors.append(
                    issue(
                        "discount_allocation_mismatch",
                        ["line_items", "discount_total"],
                    )
                )
            else:
                expected -= max(Decimal(0), remaining_discount)
                if abs(expected - total) > tolerance:
                    errors.append(
                        issue(
                            "line_items_total_mismatch", ["line_items", "grand_total"]
                        )
                    )
    return errors
