"""Tabular views of the parsed payment diaries.

The lender ships every loan's diary as one text cell. After parsing, nothing should live in a
blob any more: ``payments_frame`` is the diary as a long table (one row per diary record, typed
columns, derived fields, convention tags), and ``diary_coverage_frame`` is one row per loan
saying how much of the diary survived the export, what it spans, what it sums to and how the
borrower behaved. Both are written next to the loan report so downstream users (a warehouse,
a scoring engine, an analyst) get columns, not strings.

The payments table is the relational child of the loan header: ``(tape_id, loan_id,
payment_seq)`` is its key and ``loan_id`` is the foreign key back to the summary row. Every
derived cash column follows the same row selection and part-payment convention as the rules
(``paid_portion``, schedule rows + settlement cash reduce principal), so the tables reconcile
with the report rather than telling a second story.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Literal

import pandas as pd

from loan_dq.config import Config
from loan_dq.ingest.model import SCHEDULE_TYPES, Loan, Payment, PaymentType
from loan_dq.report.schema import public_category, public_error, public_identifier
from loan_dq.rules.base import LoanContext, paid_portion, pending_portion
from loan_dq.rules.behaviour import build_profile

PAYMENT_COLUMNS = [
    "tape_id",
    "loan_id",
    "source_row_index",
    "payment_seq",
    "instalment_no",
    "period_no",
    "event_kind",
    "due_date",
    "due_year_month",
    "due_day_of_month",
    "actual_date",
    "actual_year_month",
    "actual_day_of_month",
    "delay_days",
    "days_past_due",
    "delay_bucket",
    "is_late",
    "is_overdue",
    "is_future_scheduled",
    "type",
    "type_raw",
    "state",
    "state_raw",
    "is_paid",
    "amount_eur",
    "pending_eur",
    "paid_eur",
    "reduces_principal",
    "cum_principal_paid_eur",
    "principal_outstanding_after_eur",
    "implied_annual_rate_pct",
    "is_regular_cash",
    "is_closure_row",
    "is_settlement_marker",
    "is_aborted_settlement",
    "is_duplicate",
    "record_loan_id",
    "record_loan_id_matches",
    "parse_issues",
]

COVERAGE_COLUMNS = [
    "tape_id",
    "as_of_date",
    "loan_id",
    "source_row_index",
    "loan_status",
    "loan_type",
    "loan_amount_eur",
    "interest_rate_pct",
    "loan_term_months",
    "disbursal_date",
    "expected_repayment_date",
    "diary_readable",
    "diary_truncated",
    "diary_raw_chars",
    "records_parsed",
    "records_salvaged",
    "dropped_tail_chars",
    "parse_error",
    "regular_rows",
    "paid_rows",
    "open_rows",
    "closure_rows",
    "duplicate_rows",
    "early_repayment_rows",
    "termination_rows",
    "rows_with_parse_issues",
    "periods_in_diary",
    "periods_with_interest_row",
    "periods_with_principal_row",
    "periods_interest_without_principal",
    "first_due_date",
    "last_due_date",
    "schedule_months_beyond_diary",
    "first_actual_date",
    "last_actual_date",
    "next_due_date",
    "overdue_open_rows",
    "overdue_open_eur",
    "max_days_past_due",
    "paid_late_rows",
    "paid_late_share",
    "median_delay_days",
    "worst_delay_days",
    "longest_late_streak",
    "delay_trend_days",
    "usual_due_day",
    "usual_paid_day",
    "principal_paid_eur",
    "principal_via_closure_eur",
    "fee_paid_eur",
    "interest_paid_eur",
    "overdue_interest_paid_eur",
    "settlement_cash_eur",
    "pending_eur",
    "principal_outstanding_end_eur",
    "summary_repaid_principal_eur",
    "summary_outstanding_principal_eur",
    "summary_repaid_interest_eur",
    "summary_days_late",
]


@dataclass(frozen=True)
class TableContext:
    """What the tables need beyond the loan itself: the as-of date the report was evaluated
    against, the config (grace days, trend window) and a short id of the input file."""

    as_of: date
    config: Config
    tape_id: str

    @staticmethod
    def for_tape(as_of: date, config: Config, input_sha256: str) -> TableContext:
        return TableContext(as_of=as_of, config=config, tape_id=input_sha256[:12])


def _issues(p: Payment) -> str:
    return "; ".join(f"{i.field}: invalid or missing field" for i in p.issues)


def _year_month(d: date | None) -> str | None:
    return None if d is None else f"{d.year:04d}-{d.month:02d}"


def _day(d: date | None) -> int | None:
    return None if d is None else d.day


def _paid(p: Payment) -> float | None:
    return None if p.amount is None else round(paid_portion(p), 2)


def _money(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _event_kind(p: Payment) -> str:
    if p.is_duplicate:
        return "duplicate"
    if p.is_closure_row:
        return "closure"
    if p.is_settlement_marker:
        return "settlement_marker"
    if p.is_aborted_settlement:
        return "aborted_settlement"
    if p.type in (PaymentType.FULL_EARLY, PaymentType.PARTIAL_EARLY):
        return "early_repayment"
    if p.type is PaymentType.TERMINATION:
        return "termination"
    if p.is_regular:
        return "schedule"
    return "unknown"


def _bucket(days: int, prefix: str = "") -> str:
    if days < 0:
        return "early"
    if days == 0:
        return "on_time" if not prefix else "not_due"
    if days < 30:
        return f"{prefix}1-29"
    if days < 60:
        return f"{prefix}30-59"
    if days < 90:
        return f"{prefix}60-89"
    return f"{prefix}90+"


def _settle_date(p: Payment) -> date | None:
    return p.actual_date if p.state.is_paid and p.actual_date else p.due_date


def _principal_reductions(ctx: LoanContext) -> list[Payment]:
    """Rows whose cash reduces principal, in the order the rules treat them: schedule rows
    (principal + contract fee) and real settlement cash, chronologically by settle date."""
    rows = [p for p in ctx.schedule_rows + ctx.settlement_cash if _settle_date(p) is not None]
    return sorted(rows, key=lambda p: (_settle_date(p) or ctx.as_of, p.index))


def payment_rows(loan: Loan, tables: TableContext) -> list[dict[str, object]]:
    """One dict per diary record, in diary order, with derived fields and convention tags."""
    ctx = LoanContext(loan, tables.config, tables.as_of)
    grace = tables.config.lateness.grace_days
    loan_id_text = str(loan.loan_id) if loan.loan_id is not None else None

    reductions = _principal_reductions(ctx)
    reducers = {p.index for p in reductions}
    cum_after: dict[int, float] = {}
    running = 0.0
    reduction_dates: list[date] = []
    prefix_paid = [0.0]
    for p in reductions:
        running += paid_portion(p)
        cum_after[p.index] = running
        reduction_dates.append(_settle_date(p) or tables.as_of)
        prefix_paid.append(running)

    def balance_before(day: date) -> float | None:
        if loan.loan_amount is None:
            return None
        paid = prefix_paid[bisect_left(reduction_dates, day)]
        return loan.loan_amount - paid

    period_of: dict[date, int] = {}
    for p in sorted(
        (p for p in loan.diary.payments if p.is_regular and p.due_date),
        key=lambda p: p.due_date or tables.as_of,
    ):
        assert p.due_date is not None
        period_of.setdefault(p.due_date, len(period_of) + 1)

    rows: list[dict[str, object]] = []
    instalment_no = 0
    for p in loan.diary.payments:
        is_instalment = p.type in SCHEDULE_TYPES and p.is_regular
        if is_instalment:
            instalment_no += 1
        paid = p.state.is_paid
        delay = p.delay_days
        dpd: int | None = None
        if not paid and p.due_date is not None:
            dpd = max(0, (tables.as_of - p.due_date).days)
        bucket: str | None = None
        if paid and delay is not None:
            bucket = _bucket(delay)
        elif dpd is not None:
            bucket = _bucket(dpd, prefix="overdue_")
        implied_rate: float | None = None
        if (
            p.type is PaymentType.INTEREST
            and p.is_regular
            and p.due_date is not None
            and p.amount is not None
            and loan.interest_rate is not None
            and not loan.is_deferred_annuity
        ):
            before = balance_before(p.due_date)
            if before is not None and before > 0:
                implied_rate = round(p.amount * 12.0 / before * 100.0, 2)
        reduces = p.index in reducers
        cum = cum_after.get(p.index) if reduces else None
        rows.append(
            {
                "tape_id": tables.tape_id,
                "loan_id": loan.loan_id,
                "source_row_index": loan.row_index,
                "payment_seq": p.index,
                "instalment_no": instalment_no if is_instalment else None,
                "period_no": period_of.get(p.due_date) if p.is_regular and p.due_date else None,
                "event_kind": _event_kind(p),
                "due_date": p.due_date,
                "due_year_month": _year_month(p.due_date),
                "due_day_of_month": _day(p.due_date),
                "actual_date": p.actual_date,
                "actual_year_month": _year_month(p.actual_date),
                "actual_day_of_month": _day(p.actual_date),
                "delay_days": delay,
                "days_past_due": dpd,
                "delay_bucket": bucket,
                "is_late": paid and delay is not None and delay > grace,
                "is_overdue": dpd is not None and dpd > 0,
                "is_future_scheduled": (
                    not paid and p.due_date is not None and p.due_date > tables.as_of
                ),
                "type": p.type.value,
                "type_raw": p.type.value,
                "state": p.state.value,
                "state_raw": p.state.value,
                "is_paid": paid,
                "amount_eur": p.amount,
                "pending_eur": p.pending_amount,
                "paid_eur": _paid(p),
                "reduces_principal": reduces,
                "cum_principal_paid_eur": _money(cum),
                "principal_outstanding_after_eur": (
                    None
                    if cum is None or loan.loan_amount is None
                    else round(loan.loan_amount - cum, 2)
                ),
                "implied_annual_rate_pct": implied_rate,
                "is_regular_cash": p.is_regular,
                "is_closure_row": p.is_closure_row,
                "is_settlement_marker": p.is_settlement_marker,
                "is_aborted_settlement": p.is_aborted_settlement,
                "is_duplicate": p.is_duplicate,
                "record_loan_id": (
                    None if p.loan_id_raw is None else public_identifier(p.loan_id_raw)
                ),
                "record_loan_id_matches": (
                    None if p.loan_id_raw is None else p.loan_id_raw == loan_id_text
                ),
                "parse_issues": _issues(p),
            }
        )
    return rows


def payments_frame(loans: list[Loan], tables: TableContext) -> pd.DataFrame:
    rows = [row for loan in loans for row in payment_rows(loan, tables)]
    return _typed_frame(rows, PAYMENT_COLUMNS)


def _months_between(start: date | None, end: date | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, (end.year - start.year) * 12 + end.month - start.month)


def coverage_row(loan: Loan, tables: TableContext) -> dict[str, object]:
    d = loan.diary
    ctx = LoanContext(loan, tables.config, tables.as_of)
    regular = ctx.regular
    counted = [p for p in d.payments if not p.is_duplicate]
    dues = [p.due_date for p in d.payments if p.due_date is not None]
    actuals = [p.actual_date for p in d.payments if p.actual_date is not None]
    open_rows = [p for p in regular if not p.state.is_paid]
    overdue = [(p, dpd) for p in open_rows if (dpd := ctx.days_past_due(p)) is not None and dpd > 0]
    upcoming = [p.due_date for p in open_rows if p.due_date and p.due_date >= tables.as_of]
    profile = build_profile(ctx)
    grace = tables.config.lateness.grace_days
    paid_delays = [delay for _, delay in ctx.paid_delays]
    late = [delay for delay in paid_delays if delay > grace]

    interest_periods = {
        p.due_date for p in regular if p.type is PaymentType.INTEREST and p.due_date
    }
    principal_periods = {
        p.due_date for p in regular if p.type is PaymentType.PRINCIPAL and p.due_date
    }
    principal_paid = _paid_sum(counted, PaymentType.PRINCIPAL)
    fee_paid = _paid_sum(counted, PaymentType.FEE)
    settlement_cash = round(sum(paid_portion(p) for p in ctx.settlement_cash), 2)
    last_due = max(dues) if dues else None
    return {
        "tape_id": tables.tape_id,
        "as_of_date": tables.as_of,
        "loan_id": loan.loan_id,
        "source_row_index": loan.row_index,
        "loan_status": public_category("Loan status", loan.loan_status),
        "loan_type": public_category("Loan type", loan.loan_type),
        "loan_amount_eur": loan.loan_amount,
        "interest_rate_pct": loan.interest_rate,
        "loan_term_months": loan.loan_term_months,
        "disbursal_date": loan.disbursal_date,
        "expected_repayment_date": loan.expected_repayment_date,
        "diary_readable": d.readable,
        "diary_truncated": d.truncated,
        "diary_raw_chars": d.raw_length,
        "records_parsed": len(d.payments),
        "records_salvaged": d.salvaged_records,
        "dropped_tail_chars": d.dropped_tail_chars,
        "parse_error": public_error(d.parse_error),
        "regular_rows": len(regular),
        "paid_rows": sum(1 for p in regular if p.state.is_paid),
        "open_rows": len(open_rows),
        "closure_rows": sum(1 for p in d.payments if p.is_closure_row),
        "duplicate_rows": sum(1 for p in d.payments if p.is_duplicate),
        "early_repayment_rows": sum(
            1
            for p in ctx.settlement_cash
            if p.type in (PaymentType.FULL_EARLY, PaymentType.PARTIAL_EARLY)
        ),
        "termination_rows": sum(
            1 for p in ctx.settlement_cash if p.type is PaymentType.TERMINATION
        ),
        "rows_with_parse_issues": sum(1 for p in d.payments if p.issues),
        "periods_in_diary": len({p.due_date for p in regular if p.due_date is not None}),
        "periods_with_interest_row": len(interest_periods),
        "periods_with_principal_row": len(principal_periods),
        "periods_interest_without_principal": len(interest_periods - principal_periods),
        "first_due_date": min(dues) if dues else None,
        "last_due_date": last_due,
        "schedule_months_beyond_diary": _months_between(last_due, loan.expected_repayment_date),
        "first_actual_date": min(actuals) if actuals else None,
        "last_actual_date": max(actuals) if actuals else None,
        "next_due_date": min(upcoming) if upcoming else None,
        "overdue_open_rows": len(overdue),
        "overdue_open_eur": round(sum(pending_portion(p) for p, _ in overdue), 2),
        "max_days_past_due": max((dpd for _, dpd in overdue), default=None),
        "paid_late_rows": len(late),
        "paid_late_share": round(len(late) / len(paid_delays), 3) if paid_delays else None,
        "median_delay_days": float(median(paid_delays)) if paid_delays else None,
        "worst_delay_days": max(paid_delays) if paid_delays else None,
        "longest_late_streak": profile.longest_late_streak if profile else None,
        "delay_trend_days": profile.trend_days if profile else None,
        "usual_due_day": profile.usual_due_day if profile else None,
        "usual_paid_day": profile.usual_paid_day if profile else None,
        "principal_paid_eur": principal_paid,
        "principal_via_closure_eur": round(
            sum(paid_portion(p) for p in counted if p.type in SCHEDULE_TYPES and p.is_closure_row),
            2,
        ),
        "fee_paid_eur": fee_paid,
        "interest_paid_eur": _paid_sum(counted, PaymentType.INTEREST),
        "overdue_interest_paid_eur": _paid_sum(counted, PaymentType.OVERDUE_INTEREST),
        "settlement_cash_eur": settlement_cash,
        "pending_eur": round(sum(pending_portion(p) for p in regular), 2),
        "principal_outstanding_end_eur": (
            None
            if loan.loan_amount is None
            else round(loan.loan_amount - principal_paid - fee_paid - settlement_cash, 2)
        ),
        "summary_repaid_principal_eur": loan.repaid_principal,
        "summary_outstanding_principal_eur": loan.outstanding_principal,
        "summary_repaid_interest_eur": loan.repaid_interest,
        "summary_days_late": loan.days_late,
    }


def _paid_sum(payments: list[Payment], ptype: PaymentType) -> float:
    return round(sum(paid_portion(p) for p in payments if p.type is ptype), 2)


def diary_coverage_frame(loans: list[Loan], tables: TableContext) -> pd.DataFrame:
    return _typed_frame([coverage_row(loan, tables) for loan in loans], COVERAGE_COLUMNS)


def _typed_frame(rows: list[dict[str, object]], columns: list[str]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=columns)
    strings = {
        "tape_id",
        "loan_status",
        "loan_type",
        "event_kind",
        "due_year_month",
        "actual_year_month",
        "delay_bucket",
        "type",
        "type_raw",
        "state",
        "state_raw",
        "record_loan_id",
        "parse_issues",
        "parse_error",
    }
    booleans = {"reduces_principal", "record_loan_id_matches", "diary_readable", "diary_truncated"}
    floats = {"paid_late_share", "median_delay_days", "delay_trend_days"}
    dtype: Literal["string", "boolean", "Float64", "Int64"]
    for column in columns:
        if column.endswith("_date"):
            continue
        if column in strings:
            dtype = "string"
        elif column.startswith("is_") or column in booleans:
            dtype = "boolean"
        elif column.endswith(("_eur", "_pct")) or column in floats:
            dtype = "Float64"
        else:
            dtype = "Int64"
        frame[column] = frame[column].astype(dtype)
    return frame
