"""Scalar coercion helpers: dates in several formats, numbers, identifiers.

Every function returns ``None`` (plus a reason) instead of raising, so a single bad cell
degrades one field of one loan rather than the run.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import pandas as pd

ISO_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
DAY_FIRST = "%d/%m/%Y"
MONTH_FIRST = "%m/%d/%Y"
_SLASH_DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


@dataclass(frozen=True)
class DateParse:
    value: date | None
    fmt: str | None
    alternatives: tuple[date, ...] = ()
    error: str | None = None

    @property
    def ambiguous(self) -> bool:
        return len(self.alternatives) > 1


def is_missing(raw: object) -> bool:
    if raw is None or raw is pd.NA or raw is pd.NaT:
        return True
    if pd.api.types.is_scalar(raw) and bool(pd.isna(cast(Any, raw))):
        return True
    return isinstance(raw, str) and raw.strip().lower() in {"", "nan", "none", "null", "nat"}


def parse_date(raw: object, *, prefer_day_first: bool = True) -> DateParse:
    """Parse ISO or slash dates. Ambiguous slash dates keep both readings.

    ``value`` is the preferred reading (day-first by default, matching this lender's
    diary), ``alternatives`` lists every reading that parses so a caller can evaluate a
    date rule under each and flag only if all readings fail.
    """
    if is_missing(raw):
        return DateParse(None, None)
    if isinstance(raw, datetime):
        return DateParse(raw.date(), "datetime")
    if isinstance(raw, date):
        return DateParse(raw, "date")
    text = str(raw).strip()
    for fmt in ISO_FORMATS:
        try:
            return DateParse(datetime.strptime(text, fmt).date(), "iso")
        except ValueError:
            continue
    if _SLASH_DATE.match(text):
        readings: list[tuple[str, date]] = []
        for fmt in (DAY_FIRST, MONTH_FIRST):
            try:
                readings.append((fmt, datetime.strptime(text, fmt).date()))
            except ValueError:
                continue
        if not readings:
            return DateParse(None, None, error="unparseable slash date")
        if len(readings) == 2 and readings[0][1] != readings[1][1]:
            order = readings if prefer_day_first else list(reversed(readings))
            return DateParse(order[0][1], order[0][0], tuple(r[1] for r in order))
        return DateParse(readings[0][1], readings[0][0])
    return DateParse(None, None, error="unrecognised date format")


def _numeric_text(raw: object) -> tuple[str | None, str | None]:
    if is_missing(raw):
        return None, None
    if isinstance(raw, bool):
        return None, "boolean where number expected"
    text = str(raw).strip().replace("\u00a0", " ")
    text = re.sub(r"^(?:EUR|€)\s*|\s*(?:EUR|€)$", "", text).strip()
    if " " in text:
        if not re.fullmatch(r"[+-]?\d{1,3}(?: \d{3})+(?:[.,]\d+)?", text):
            return None, "invalid numeric grouping"
        text = text.replace(" ", "")
    if "," in text and "." in text:
        decimal, grouping = (",", ".") if text.rfind(",") > text.rfind(".") else (".", ",")
        pattern = rf"[+-]?\d{{1,3}}(?:{re.escape(grouping)}\d{{3}})+{re.escape(decimal)}\d+"
        if not re.fullmatch(pattern, text):
            return None, "invalid numeric grouping"
        text = text.replace(grouping, "").replace(decimal, ".")
    elif text.count(",") > 1 or text.count(".") > 1:
        grouping = "," if "," in text else "."
        if not re.fullmatch(rf"[+-]?\d{{1,3}}(?:{re.escape(grouping)}\d{{3}})+", text):
            return None, "invalid numeric grouping"
        text = text.replace(grouping, "")
    elif "," in text:
        if re.fullmatch(r"[+-]?\d{1,3},\d{3}", text):
            return None, "ambiguous numeric separator"
        text = text.replace(",", ".")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        return None, "not a finite number"
    return text, None


def parse_float(raw: object) -> tuple[float | None, str | None]:
    text, err = _numeric_text(raw)
    if text is None:
        return None, err
    try:
        value = float(text)
    except (ValueError, OverflowError):
        return None, "not a number"
    return (value, None) if math.isfinite(value) else (None, "non-finite number")


def parse_int(raw: object) -> tuple[int | None, str | None]:
    text, err = _numeric_text(raw)
    if text is None:
        return None, err
    try:
        value = Decimal(text)
        if not value.is_finite() or value.adjusted() > 1000:
            return None, "integer outside supported range"
        if value != value.to_integral_value():
            return None, "not an integer"
        return int(value), None
    except (InvalidOperation, ValueError, OverflowError):
        return None, "not an integer"


def parse_identifier(raw: object) -> tuple[int | None, str]:
    """Loan/Borrower IDs appear as int in the parent row and str in the diary."""
    if is_missing(raw):
        return None, ""
    text = str(raw).strip()
    if text.endswith(".0"):
        text = text[:-2]
    try:
        return int(text), text
    except ValueError:
        return None, text


def parse_text(raw: object) -> str | None:
    if is_missing(raw):
        return None
    return str(raw).strip()
