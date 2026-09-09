"""Typed, parsed representation of one loan: the summary row plus its payment diary.

Ingestion turns each raw spreadsheet row into a ``Loan``. Fields that failed to parse are
``None`` and the failure is recorded in ``parse_issues`` so downstream rules can return
``not_evaluable`` instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class PaymentType(str, Enum):
    PRINCIPAL = "principal"
    INTEREST = "interest"
    FEE = "contract fee repayment"
    OVERDUE_INTEREST = "overdue interest"
    FULL_EARLY = "full early repayment"
    PARTIAL_EARLY = "partial early repayment"
    TERMINATION = "repayment after agreement termination"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: object) -> PaymentType:
        text = str(raw).strip().lower() if raw is not None else ""
        for member in cls:
            if member.value == text:
                return member
        return cls.UNKNOWN


class PaymentState(str, Enum):
    PAID_ON_TIME = "paid on time"
    PAID_WITH_DELAY = "paid with delay"
    PENDING = "pending"
    PENDING_LATE = "pending late"
    GRACE = "payment in grace period"
    PENDING_TODAY = "payment pending today"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: object) -> PaymentState:
        text = str(raw).strip().lower() if raw is not None else ""
        for member in cls:
            if member.value == text:
                return member
        return cls.UNKNOWN

    @property
    def is_paid(self) -> bool:
        return self in (PaymentState.PAID_ON_TIME, PaymentState.PAID_WITH_DELAY)


SCHEDULE_TYPES = frozenset({PaymentType.PRINCIPAL, PaymentType.FEE})
INTEREST_TYPES = frozenset({PaymentType.INTEREST, PaymentType.OVERDUE_INTEREST})
SETTLEMENT_TYPES = frozenset(
    {PaymentType.FULL_EARLY, PaymentType.PARTIAL_EARLY, PaymentType.TERMINATION}
)


@dataclass
class ParseIssue:
    field: str
    problem: str
    raw: str | None = None


@dataclass
class Payment:
    """One line of the payment diary."""

    index: int
    loan_id_raw: str | None
    due_date: date | None
    actual_date: date | None
    type: PaymentType
    state: PaymentState
    amount: float | None
    pending_amount: float | None
    type_raw: str
    state_raw: str
    date_formats: tuple[str, ...] = ()
    issues: list[ParseIssue] = field(default_factory=list)
    # Tags set by diary post-processing (see ingest/diary.py)
    is_closure_row: bool = False
    is_duplicate: bool = False
    is_settlement_marker: bool = False
    is_aborted_settlement: bool = False

    @property
    def delay_days(self) -> int | None:
        if self.due_date is None or self.actual_date is None:
            return None
        return (self.actual_date - self.due_date).days

    @property
    def is_regular(self) -> bool:
        """A schedule/interest row that carries real cash and should enter sums/lateness."""
        return (
            not self.is_closure_row
            and not self.is_duplicate
            and self.type in SCHEDULE_TYPES | INTEREST_TYPES
        )


@dataclass
class Diary:
    payments: list[Payment]
    raw_length: int
    truncated: bool = False
    parse_error: str | None = None
    salvaged_records: int = 0
    dropped_tail_chars: int = 0

    @property
    def readable(self) -> bool:
        return self.parse_error is None

    @property
    def complete(self) -> bool:
        return self.readable and not self.truncated


@dataclass
class Loan:
    row_index: int
    loan_id: int | None
    loan_id_raw: str
    borrower_id: str | None
    loan_amount: float | None
    disbursal_date: date | None
    interest_rate: float | None
    loan_term_months: int | None
    expected_repayment_date: date | None
    loan_type: str | None
    borrower_type: str | None
    credit_score: str | None
    monthly_payment: float | None
    loan_status: str | None
    days_late: int | None
    outstanding_principal: float | None
    repaid_principal: float | None
    outstanding_interest: float | None
    repaid_interest: float | None
    repayment_date: date | None
    arrears: float | None
    delay_interest: float | None
    purpose: str | None
    # Plausibility inputs: values are consumed by H rules but never copied into findings.
    birth_year: int | None
    family_income: float | None
    borrower_income: float | None
    family_liabilities: float | None
    children: float | None
    employment_status: str | None
    company_age_years: float | None
    diary: Diary
    parse_issues: list[ParseIssue] = field(default_factory=list)
    ambiguous_dates: dict[str, list[date]] = field(default_factory=dict)
    missing_optional_columns: list[str] = field(default_factory=list)

    @property
    def critical_evidence_complete(self) -> bool:
        return (
            self.borrower_id is not None
            and self.loan_amount is not None
            and self.loan_amount > 0
            and self.interest_rate is not None
            and self.interest_rate >= 0
            and self.loan_term_months is not None
            and self.loan_term_months > 0
            and self.disbursal_date is not None
            and self.loan_status is not None
            and not self.parse_issues
            and self.diary.complete
            and bool(self.diary.payments)
            and all(
                not p.issues
                and p.loan_id_raw is not None
                and p.due_date is not None
                and p.amount is not None
                and p.pending_amount is not None
                and 0 <= p.pending_amount <= p.amount
                and (not p.state.is_paid or p.actual_date is not None)
                for p in self.diary.payments
            )
        )

    @property
    def is_live(self) -> bool:
        return self.loan_status == "granted"

    @property
    def is_repaid(self) -> bool:
        return self.loan_status == "repaid"

    @property
    def is_terminated(self) -> bool:
        return self.loan_status == "terminated"

    @property
    def is_deferred_annuity(self) -> bool:
        return self.loan_type == "deferred annuity"
