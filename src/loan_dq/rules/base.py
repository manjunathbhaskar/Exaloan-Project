"""Rule protocol and the three-valued outcome every rule returns.

A rule is a small class with an ``id``, ``category``, default ``severity``, the ``axis`` it
reports on (``integrity`` = the tape contradicts itself; ``credit`` = the borrower was
genuinely late) and an ``evaluate`` method returning exactly one ``Outcome``:

* ``PASS``          - the invariant holds.
* ``FAIL``          - it does not; ``message`` and ``evidence`` say why (the two values that
                      disagreed, never PII).
* ``NOT_EVALUABLE`` - the inputs needed are missing/truncated/unknown-product. Never a flag.

``subsumed_by`` lists rule IDs whose failure explains this one (root-cause de-duplication):
if one of them also failed on the same loan, this finding is attached as a symptom.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from functools import cached_property
from typing import ClassVar, Protocol

from loan_dq.config import Config
from loan_dq.ingest.model import (
    INTEREST_TYPES,
    SCHEDULE_TYPES,
    Loan,
    Payment,
    PaymentState,
    PaymentType,
)


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _RANK[self]

    @property
    def counts_as_flag(self) -> bool:
        return self is not Severity.INFO


_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


def max_severity(items: list[Severity]) -> Severity | None:
    return max(items, key=lambda s: s.rank) if items else None


class CheckGroup(str, Enum):
    RECORD_CONSISTENCY = "record_consistency"
    REPAYMENT_BEHAVIOUR = "repayment_behaviour"
    WHOLE_TAPE = "whole_tape"

    @property
    def label(self) -> str:
        return {
            CheckGroup.RECORD_CONSISTENCY: "Record consistency",
            CheckGroup.REPAYMENT_BEHAVIOUR: "Repayment behaviour",
            CheckGroup.WHOLE_TAPE: "Whole-tape quality",
        }[self]


def check_group(rule_id: str) -> CheckGroup | None:
    prefix = rule_id[:1]
    if rule_id == "C8" or prefix in {"F", "G"}:
        return CheckGroup.REPAYMENT_BEHAVIOUR
    if prefix in {"A", "B", "C", "D", "E", "H"}:
        return CheckGroup.RECORD_CONSISTENCY
    if prefix == "I":
        return CheckGroup.WHOLE_TAPE
    return None


class Axis(str, Enum):
    INTEGRITY = "integrity"
    CREDIT = "credit"
    COVERAGE = "coverage"


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_EVALUABLE = "not_evaluable"


Evidence = dict[str, object]


@dataclass
class Outcome:
    status: Status
    message: str = ""
    evidence: Evidence = field(default_factory=dict)
    severity: Severity | None = None  # override the rule default (e.g. F1 live vs repaid)
    standalone: bool = False  # never demote to a symptom, even if a subsuming rule failed

    @classmethod
    def ok(cls, evidence: Evidence | None = None) -> Outcome:
        return cls(Status.PASS, evidence=evidence or {})

    @classmethod
    def fail(
        cls,
        message: str,
        evidence: Evidence | None = None,
        severity: Severity | None = None,
        *,
        standalone: bool = False,
    ) -> Outcome:
        return cls(Status.FAIL, message, evidence or {}, severity, standalone)

    @classmethod
    def skip(cls, reason: str) -> Outcome:
        return cls(Status.NOT_EVALUABLE, reason)


class Rule(Protocol):
    id: ClassVar[str]
    category: ClassVar[str]
    severity: ClassVar[Severity]
    axis: ClassVar[Axis]
    title: ClassVar[str]
    subsumed_by: ClassVar[tuple[str, ...]]

    def evaluate(self, ctx: LoanContext) -> Outcome: ...


class BaseRule:
    """Convenience base: subclasses set the class attributes and implement ``evaluate``."""

    id: ClassVar[str] = ""
    category: ClassVar[str] = ""
    severity: ClassVar[Severity] = Severity.MEDIUM
    axis: ClassVar[Axis] = Axis.INTEGRITY
    title: ClassVar[str] = ""
    subsumed_by: ClassVar[tuple[str, ...]] = ()

    def evaluate(self, ctx: LoanContext) -> Outcome:
        raise NotImplementedError


class LoanContext:
    """One loan plus config/as-of and cached derived views shared by rules."""

    def __init__(self, loan: Loan, config: Config, as_of: date) -> None:
        self.loan = loan
        self.config = config
        self.as_of = as_of

    @property
    def diary(self) -> list[Payment]:
        return self.loan.diary.payments

    @cached_property
    def regular(self) -> list[Payment]:
        """Schedule + interest rows that carry cash (no closure rows, no duplicates)."""
        return [p for p in self.diary if p.is_regular]

    @cached_property
    def schedule_rows(self) -> list[Payment]:
        return [p for p in self.diary if p.type in SCHEDULE_TYPES and not p.is_duplicate]

    @cached_property
    def interest_rows(self) -> list[Payment]:
        return [p for p in self.diary if p.type in INTEREST_TYPES and not p.is_duplicate]

    @cached_property
    def settlement_cash(self) -> list[Payment]:
        """Early/termination repayment rows that are real cash (not mirrors of closure rows)."""
        return [
            p
            for p in self.diary
            if p.type
            in (PaymentType.FULL_EARLY, PaymentType.PARTIAL_EARLY, PaymentType.TERMINATION)
            and not p.is_settlement_marker
            and not p.is_aborted_settlement
        ]

    @cached_property
    def lateness_rows(self) -> list[Payment]:
        """Rows whose due/actual pair measures borrower lateness."""
        return [
            p
            for p in self.regular
            if p.type in (PaymentType.PRINCIPAL, PaymentType.INTEREST, PaymentType.FEE)
            and p.due_date is not None
        ]

    @cached_property
    def paid_delays(self) -> list[tuple[Payment, int]]:
        out: list[tuple[Payment, int]] = []
        for p in self.lateness_rows:
            d = p.delay_days
            if paid_portion(p) > 0.0 and d is not None:
                out.append((p, d))
        return out

    @cached_property
    def unpaid_rows(self) -> list[Payment]:
        return [
            p
            for p in self.lateness_rows
            if pending_portion(p) > 0.0 and p.state is not PaymentState.UNKNOWN
        ]

    def days_past_due(self, p: Payment) -> int | None:
        if p.due_date is None:
            return None
        return (self.as_of - p.due_date).days

    @property
    def diary_ok(self) -> bool:
        return self.loan.diary.readable and bool(self.diary)

    @property
    def diary_complete(self) -> bool:
        return self.loan.diary.complete and bool(self.diary)

    def skip_if_diary_unusable(self, need_complete: bool) -> Outcome | None:
        if not self.loan.diary.readable:
            return Outcome.skip("payment diary unreadable")
        if not self.diary:
            return Outcome.skip("payment diary empty")
        if need_complete and self.loan.diary.truncated:
            return Outcome.skip(
                "payment diary truncated at Excel cell limit; sums are lower bounds"
            )
        return None


def money(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def total(rows: list[Payment]) -> float:
    return sum(p.amount or 0.0 for p in rows)


def paid_portion(p: Payment) -> float:
    """Implied receipts are amount minus sane pending, regardless of the paid label.
    Use the state only as a fallback when no valid pending amount is available."""
    if p.amount is None or p.is_settlement_marker or p.is_aborted_settlement or p.is_duplicate:
        return 0.0
    if p.pending_amount is not None and 0.0 <= p.pending_amount <= p.amount:
        return p.amount - p.pending_amount
    if p.state.is_paid:
        return p.amount
    return 0.0


def pending_portion(p: Payment) -> float:
    """Remaining exposure uses sane pending even when the state claims the row is paid.
    Fall back to the state only when no valid pending amount is available."""
    if p.amount is None or p.is_settlement_marker or p.is_aborted_settlement or p.is_duplicate:
        return 0.0
    if p.pending_amount is not None and 0.0 <= p.pending_amount <= p.amount:
        return p.pending_amount
    return 0.0 if p.state.is_paid else p.amount


def pending_total(rows: list[Payment]) -> float:
    return sum(pending_portion(p) for p in rows)


def due_dates(rows: list[Payment]) -> int:
    """Distinct due dates among the rows: the number of instalment episodes behind a set of
    component rows (principal, interest and fee of one instalment share a due date)."""
    return len({p.due_date for p in rows if p.due_date is not None})
