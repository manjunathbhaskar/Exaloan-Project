"""Shared fixtures: a synthetic clean loan factory and the real workbook.

The factory builds a spreadsheet row exactly as the lender exports it (a summary plus a
``payments`` diary string) so that every test exercises the real ingestion path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from loan_dq.config import Config
from loan_dq.engine import evaluate_loan
from loan_dq.ingest.model import Loan
from loan_dq.ingest.schema import ingest
from loan_dq.report.schema import LoanResult
from loan_dq.rules.base import Rule
from loan_dq.rules.registry import all_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = REPO_ROOT / "data" / "loans.xlsx"
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"


def add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(d.day, 28))


def fmt(d: date | None) -> str | None:
    return None if d is None else d.strftime("%d/%m/%Y")


@dataclass
class DiaryRow:
    due: date
    paid: date | None
    type: str
    state: str
    amount: float
    pending: float = 0.0

    def as_record(self, loan_id: str) -> dict[str, object]:
        return {
            "Loan ID": loan_id,
            "Payment date": fmt(self.due),
            "Repayment date": fmt(self.paid),
            "Type": self.type,
            "State": self.state,
            "Amount": round(self.amount, 2),
            "Pending amount": round(self.pending, 2),
        }


@dataclass
class SyntheticLoan:
    """A clean, amortising instalment loan that every rule should pass.

    ``paid_through`` instalments are paid on their due date; the rest are still pending.
    ``as_of`` is the export date used for pending-row evaluation.
    """

    loan_id: int = 10000001
    borrower_id: int = 555
    amount: float = 1200.0
    annual_rate: float = 12.0
    term: int = 12
    disbursed: date = date(2024, 1, 15)
    paid_through: int = 12
    as_of: date = date(2025, 2, 1)
    rows: list[DiaryRow] = field(default_factory=list)
    summary_overrides: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.rows:
            self.rows = self._schedule()

    @property
    def is_repaid(self) -> bool:
        return self.paid_through >= self.term

    def _schedule(self) -> list[DiaryRow]:
        r = self.annual_rate / 100.0 / 12.0
        pmt = self.amount * r / (1 - (1 + r) ** -self.term)
        balance = self.amount
        rows: list[DiaryRow] = []
        for k in range(1, self.term + 1):
            due = add_months(self.disbursed, k)
            interest = round(balance * r, 2)
            principal = round(pmt - interest, 2) if k < self.term else round(balance, 2)
            balance = round(balance - principal, 2)
            paid = k <= self.paid_through
            state = "paid on time" if paid else "pending"
            when = due if paid else None
            rows.append(
                DiaryRow(due, when, "principal", state, principal, 0 if paid else principal)
            )
            rows.append(DiaryRow(due, when, "interest", state, interest, 0 if paid else interest))
        return rows

    @property
    def monthly_payment(self) -> float:
        r = self.annual_rate / 100.0 / 12.0
        return round(self.amount * r / (1 - (1 + r) ** -self.term), 2)

    def summary(self) -> dict[str, object]:
        paid_principal = sum(x.amount for x in self.rows if x.type == "principal" and x.paid)
        paid_interest = sum(x.amount for x in self.rows if x.type == "interest" and x.paid)
        pending_interest = sum(x.amount for x in self.rows if x.type == "interest" and not x.paid)
        last_due = add_months(self.disbursed, self.term)
        last_paid = max((x.paid for x in self.rows if x.paid), default=None)
        row: dict[str, object] = {
            "Unnamed: 0": 1,
            "Borrower ID": self.borrower_id,
            "Loan ID": self.loan_id,
            "Credit score": "B",
            "Loan amount": round(self.amount),
            "Disbursal date": self.disbursed.isoformat() + "T00:00:00.000",
            "Interest rate": self.annual_rate,
            "Loan term": self.term,
            "Borrower type": "individual",
            "Loan type": "instalment",
            "Expected repayment date": last_due.isoformat() + "T00:00:00.000",
            "Loan status": "repaid" if self.is_repaid else "granted",
            "Days late": 0,
            "Purpose": "refinancing",
            "Monthly payment": self.monthly_payment,
            "Outstanding principal": round(self.amount - paid_principal, 2),
            "Repaid principal": round(paid_principal, 2),
            "payments": str([x.as_record(str(self.loan_id)) for x in self.rows]),
            "Outstanding interest": round(pending_interest, 2),
            "Repaid interest": round(paid_interest, 2),
            "Repayment date": (
                last_paid.isoformat() + "T00:00:00.000" if self.is_repaid and last_paid else None
            ),
            "Arrears": None,
            "Delay interest": 0.0,
            "Birth year": 1985.0,
            "Gender": "female",
            "Children": 1.0,
            "Employment status": "employed full time",
            "Family income": 2500.0,
            "Borrower income": 1800.0,
            "Family liabilities": 200.0,
            "Company age (years)": None,
            "City": "Vilnius",
        }
        row.update(self.summary_overrides)
        return row

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame([self.summary()])


def make_frame(*loans: SyntheticLoan) -> pd.DataFrame:
    return pd.DataFrame([loan.summary() for loan in loans])


@pytest.fixture(scope="session")
def config() -> Config:
    return Config.load(DEFAULT_CONFIG)


@pytest.fixture(scope="session")
def rules() -> list[Rule]:
    return all_rules()


@dataclass
class Harness:
    config: Config
    rules: list[Rule]

    def ingest_one(self, loan: SyntheticLoan) -> Loan:
        result = ingest(loan.frame(), self.config)
        assert result.schema.ok, result.schema.required_missing
        assert not result.quarantined, result.quarantined
        assert len(result.loans) == 1
        return result.loans[0]

    def evaluate(self, loan: SyntheticLoan, as_of: date | None = None) -> LoanResult:
        parsed = self.ingest_one(loan)
        return evaluate_loan(parsed, self.config, as_of or loan.as_of, self.rules)


@pytest.fixture(scope="session")
def harness(config: Config, rules: list[Rule]) -> Harness:
    return Harness(config, rules)


def failed_ids(result: LoanResult) -> set[str]:
    return {f.rule_id for f in result.findings if f.severity != "info"}


def shift(d: date, days: int) -> date:
    return d + timedelta(days=days)
