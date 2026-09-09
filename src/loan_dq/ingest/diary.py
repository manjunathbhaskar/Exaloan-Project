"""Parse and annotate the nested payment diary stored in the ``payments`` cell.

The cell holds a Python-literal list of dicts (not JSON). Excel truncates cells at 32,767
characters, so long diaries arrive cut mid-record; we salvage every complete record and
mark the diary as truncated so sum-based rules become ``not_evaluable`` instead of firing.

After parsing, rows are tagged with the lender conventions that would otherwise look like
anomalies (the "known-quirk register" of the plan):

* **closure rows** - after a partial/full early repayment the lender keeps the future
  schedule rows, marks them ``paid on time`` and stamps them with the settlement date
  (earlier than their due date). They are not cash; the settlement row is.
* **settlement markers** - a paid early-repayment row that merely aggregates closure rows,
  or a ``pending`` early-repayment twin with pending amount 0. Excluded from cash sums.
* **aborted settlements** - a paid ``full early repayment`` after which regular instalments
  continued to be paid; the loan was not actually closed, so the row must not be treated as
  the closing payment in the principal identity.
* **duplicates** - byte-identical schedule/interest rows (only the later copy is tagged).
"""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from datetime import date

from loan_dq.ingest.model import (
    INTEREST_TYPES,
    SCHEDULE_TYPES,
    Diary,
    ParseIssue,
    Payment,
    PaymentState,
    PaymentType,
)
from loan_dq.ingest.normalize import is_missing, parse_date, parse_float, parse_identifier

EXPECTED_KEYS = frozenset(
    {"Loan ID", "Payment date", "Repayment date", "Type", "State", "Amount", "Pending amount"}
)


def _literal(text: str) -> list[object]:
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        value = json.loads(text)
    if not isinstance(value, list):
        raise ValueError(f"diary is a {type(value).__name__}, expected list")
    return value


def _salvage(text: str) -> tuple[list[object], int]:
    """Cut a truncated literal at the last complete record and close the list."""
    cut = text.rfind("}")
    if cut < 0:
        raise ValueError("no complete record found in truncated diary")
    salvaged = text[: cut + 1] + "]"
    return _literal(salvaged), len(text) - cut - 1


def parse_diary(raw: object, *, excel_cell_limit: int = 32767) -> Diary:
    if is_missing(raw):
        return Diary(payments=[], raw_length=0, parse_error="payments cell is empty")
    if isinstance(raw, list):
        return _build(raw, raw_length=-1, truncated=False, dropped=0)
    text = str(raw)
    length = len(text)
    truncated = length >= excel_cell_limit
    try:
        records = _literal(text)
        dropped = 0
    except (ValueError, SyntaxError, json.JSONDecodeError):
        try:
            records, dropped = _salvage(text)
            truncated = True
        except (ValueError, SyntaxError, json.JSONDecodeError) as exc:
            return Diary(
                payments=[],
                raw_length=length,
                truncated=truncated,
                parse_error=f"diary could not be parsed ({type(exc).__name__})",
            )
    return _build(records, raw_length=length, truncated=truncated, dropped=dropped)


def _build(records: list[object], *, raw_length: int, truncated: bool, dropped: int) -> Diary:
    payments: list[Payment] = []
    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            payments.append(_unreadable_row(idx, f"record is {type(rec).__name__}, not dict"))
            continue
        payments.append(_to_payment(idx, rec))
    tag_conventions(payments)
    return Diary(
        payments=payments,
        raw_length=raw_length,
        truncated=truncated,
        salvaged_records=len(payments) if truncated else 0,
        dropped_tail_chars=dropped,
    )


def _unreadable_row(idx: int, problem: str) -> Payment:
    return Payment(
        index=idx,
        loan_id_raw=None,
        due_date=None,
        actual_date=None,
        type=PaymentType.UNKNOWN,
        state=PaymentState.UNKNOWN,
        amount=None,
        pending_amount=None,
        type_raw="",
        state_raw="",
        issues=[ParseIssue("record", problem)],
    )


def _to_payment(idx: int, rec: dict[object, object]) -> Payment:
    issues: list[ParseIssue] = []
    keys = {str(k) for k in rec}
    missing = EXPECTED_KEYS - keys
    extra = keys - EXPECTED_KEYS
    if missing:
        issues.append(ParseIssue("keys", f"missing keys {sorted(missing)}"))
    if extra:
        issues.append(ParseIssue("keys", f"{len(extra)} unexpected keys"))

    due = parse_date(rec.get("Payment date"))
    if due.error:
        issues.append(ParseIssue("Payment date", due.error))
    elif due.value is None and "Payment date" in keys:
        issues.append(ParseIssue("Payment date", "missing"))
    actual = parse_date(rec.get("Repayment date"))
    if actual.error:
        issues.append(ParseIssue("Repayment date", actual.error))

    amount, amount_err = parse_float(rec.get("Amount"))
    if amount_err or amount is None:
        issues.append(ParseIssue("Amount", amount_err or "missing"))
    pending, pending_err = parse_float(rec.get("Pending amount"))
    if pending_err or pending is None:
        issues.append(ParseIssue("Pending amount", pending_err or "missing"))

    type_raw = "" if is_missing(rec.get("Type")) else str(rec.get("Type"))
    state_raw = "" if is_missing(rec.get("State")) else str(rec.get("State"))
    ptype = PaymentType.parse(type_raw)
    pstate = PaymentState.parse(state_raw)
    if ptype is PaymentType.UNKNOWN:
        issues.append(ParseIssue("Type", "unknown payment type"))
    if pstate is PaymentState.UNKNOWN:
        issues.append(ParseIssue("State", "unknown payment state"))

    _, loan_id_text = parse_identifier(rec.get("Loan ID"))
    return Payment(
        index=idx,
        loan_id_raw=loan_id_text or None,
        due_date=due.value,
        actual_date=actual.value,
        type=ptype,
        state=pstate,
        amount=amount,
        pending_amount=pending,
        type_raw=type_raw,
        state_raw=state_raw,
        date_formats=tuple(f for f in (due.fmt, actual.fmt) if f is not None),
        issues=issues,
    )


def tag_conventions(payments: list[Payment]) -> None:
    """Apply the known-quirk register to a parsed diary (idempotent)."""
    for p in payments:
        p.is_closure_row = p.is_duplicate = False
        p.is_settlement_marker = p.is_aborted_settlement = False

    early_paid_dates: set[date] = {
        p.actual_date
        for p in payments
        if p.type in (PaymentType.PARTIAL_EARLY, PaymentType.FULL_EARLY)
        and p.state.is_paid
        and p.actual_date is not None
    }
    closure_sum_by_date: dict[date, float] = defaultdict(float)
    for p in payments:
        if (
            p.type in SCHEDULE_TYPES | INTEREST_TYPES
            and p.due_date is not None
            and p.actual_date is not None
            and p.actual_date < p.due_date
            and p.actual_date in early_paid_dates
        ):
            p.is_closure_row = True
            if p.type in SCHEDULE_TYPES:
                closure_sum_by_date[p.actual_date] += p.amount or 0.0

    last_regular_paid: date | None = None
    for p in payments:
        if (
            p.type in SCHEDULE_TYPES
            and p.state.is_paid
            and not p.is_closure_row
            and p.actual_date
            and (last_regular_paid is None or p.actual_date > last_regular_paid)
        ):
            last_regular_paid = p.actual_date

    for p in payments:
        if (
            (p.type is PaymentType.PARTIAL_EARLY and p.actual_date in closure_sum_by_date)
            or (p.type is PaymentType.FULL_EARLY and p.actual_date in closure_sum_by_date)
            or (
                p.type in (PaymentType.FULL_EARLY, PaymentType.PARTIAL_EARLY)
                and not p.state.is_paid
                and (p.pending_amount or 0.0) == 0.0
            )
        ):
            p.is_settlement_marker = True
        elif (
            p.type is PaymentType.FULL_EARLY
            and p.state.is_paid
            and p.actual_date is not None
            and last_regular_paid is not None
            and last_regular_paid > p.actual_date
        ):
            p.is_aborted_settlement = True

    seen: set[tuple[object, ...]] = set()
    for p in payments:
        if p.type not in SCHEDULE_TYPES | {PaymentType.INTEREST}:
            continue
        key = (p.due_date, p.actual_date, p.type, p.state, p.amount, p.pending_amount)
        if key in seen:
            p.is_duplicate = True
        else:
            seen.add(key)
