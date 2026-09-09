"""Tape-level schema check and row-level conversion into ``Loan`` objects.

A tape is rejected only when a *required* column is absent. Optional columns that are
missing simply make the rules that need them ``not_evaluable``. A row that cannot be
converted is quarantined with a reason; the run continues with the next row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from loan_dq.config import Config
from loan_dq.ingest.diary import parse_diary
from loan_dq.ingest.model import Diary, Loan, ParseIssue
from loan_dq.ingest.normalize import (
    is_missing,
    parse_date,
    parse_float,
    parse_identifier,
    parse_int,
    parse_text,
)


@dataclass
class SchemaReport:
    columns_present: list[str]
    required_missing: list[str]
    optional_missing: list[str]
    unexpected: list[str]
    duplicate_columns: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.required_missing and not self.duplicate_columns


@dataclass
class QuarantinedRow:
    row_index: int
    loan_id_raw: str
    reason: str


@dataclass
class IngestResult:
    loans: list[Loan]
    quarantined: list[QuarantinedRow]
    schema: SchemaReport
    inferred_as_of: date | None = None
    # Days between the latest date in the tape and the next-latest distinct one. A large gap
    # means the inferred as-of rests on a single row - possibly a future-dated payment that
    # the inference would otherwise hide from D2.
    inferred_as_of_gap_days: int | None = None
    issues: list[str] = field(default_factory=list)


def check_schema(frame: pd.DataFrame, config: Config) -> SchemaReport:
    present = [str(c).strip() for c in frame.columns]
    required = config.schema_.required_columns
    optional = config.schema_.optional_columns
    known = set(required) | set(optional)
    return SchemaReport(
        columns_present=present,
        required_missing=[c for c in required if c not in present],
        optional_missing=[c for c in optional if c not in present],
        unexpected=[c for c in present if c not in known and not c.startswith("Unnamed:")],
        duplicate_columns=sorted({c for c in present if present.count(c) > 1}),
    )


def ingest(frame: pd.DataFrame, config: Config, *, row_offset: int = 0) -> IngestResult:
    """``row_offset`` is the tape position of the frame's first row (non-zero for a shard)."""
    schema = check_schema(frame, config)
    if not schema.ok:
        issues = ["duplicate canonical column headers"] if schema.duplicate_columns else []
        return IngestResult(loans=[], quarantined=[], schema=schema, issues=issues)
    frame = frame.copy(deep=False)
    frame.columns = schema.columns_present

    loans: list[Loan] = []
    quarantined: list[QuarantinedRow] = []
    for position, (_, row) in enumerate(frame.iterrows(), start=row_offset):
        raw_id = "" if "Loan ID" not in row else str(row["Loan ID"])
        try:
            loan = row_to_loan(position, row, schema.optional_missing, config)
        except Exception as exc:  # any unexpected shape -> quarantine, keep going
            quarantined.append(
                QuarantinedRow(position, raw_id, f"row conversion failed ({type(exc).__name__})")
            )
            continue
        if loan.loan_id is None:
            quarantined.append(
                QuarantinedRow(position, raw_id, "Loan ID missing or not an integer")
            )
            continue
        loans.append(loan)

    return IngestResult(
        loans=loans,
        quarantined=quarantined,
        schema=schema,
        inferred_as_of=infer_as_of(loans),
        inferred_as_of_gap_days=inferred_as_of_gap(loans),
    )


def _tape_dates(loans: list[Loan]) -> list[date]:
    candidates: list[date] = []
    for loan in loans:
        for d in (loan.disbursal_date, loan.repayment_date):
            if d is not None:
                candidates.append(d)
        for p in loan.diary.payments:
            if p.actual_date is not None:
                candidates.append(p.actual_date)
    return candidates


def infer_as_of(loans: list[Loan]) -> date | None:
    """Fallback as-of date: the latest date anywhere in the tape."""
    candidates = _tape_dates(loans)
    return max(candidates) if candidates else None


def inferred_as_of_gap(loans: list[Loan]) -> int | None:
    distinct = sorted(set(_tape_dates(loans)), reverse=True)
    if len(distinct) < 2:
        return None
    return (distinct[0] - distinct[1]).days


def _get(row: pd.Series, column: str, missing_optional: list[str]) -> object:
    if column in missing_optional or column not in row.index:
        return None
    return row[column]


def row_to_loan(
    position: int,
    row: pd.Series,
    missing_optional: list[str],
    config: Config,
) -> Loan:
    issues = [
        ParseIssue(column, "required value is missing")
        for column in config.schema_.required_columns
        if is_missing(row.get(column))
    ]
    ambiguous: dict[str, list[date]] = {}

    def num(col: str) -> float | None:
        value, err = parse_float(_get(row, col, missing_optional))
        if err:
            issues.append(ParseIssue(col, err))
        return value

    def integer(col: str) -> int | None:
        value, err = parse_int(_get(row, col, missing_optional))
        if err:
            issues.append(ParseIssue(col, err))
        return value

    def dt(col: str) -> date | None:
        parsed = parse_date(_get(row, col, missing_optional))
        if parsed.error:
            issues.append(ParseIssue(col, parsed.error))
        if parsed.ambiguous:
            ambiguous[col] = list(parsed.alternatives)
        return parsed.value

    def text(col: str) -> str | None:
        return parse_text(_get(row, col, missing_optional))

    loan_id, loan_id_raw = parse_identifier(row.get("Loan ID"))
    borrower_id_int, borrower_id_raw = parse_identifier(row.get("Borrower ID"))
    diary: Diary = parse_diary(row.get("payments"), excel_cell_limit=config.tape.excel_cell_limit)

    return Loan(
        row_index=position,
        loan_id=loan_id,
        loan_id_raw=loan_id_raw,
        borrower_id=borrower_id_raw or None if borrower_id_int is None else str(borrower_id_int),
        loan_amount=num("Loan amount"),
        disbursal_date=dt("Disbursal date"),
        interest_rate=num("Interest rate"),
        loan_term_months=integer("Loan term"),
        expected_repayment_date=dt("Expected repayment date"),
        loan_type=_lower(text("Loan type")),
        borrower_type=_lower(text("Borrower type")),
        credit_score=(text("Credit score") or "").upper() or None,
        monthly_payment=num("Monthly payment"),
        loan_status=_lower(text("Loan status")),
        days_late=integer("Days late"),
        outstanding_principal=num("Outstanding principal"),
        repaid_principal=num("Repaid principal"),
        outstanding_interest=num("Outstanding interest"),
        repaid_interest=num("Repaid interest"),
        repayment_date=dt("Repayment date"),
        arrears=num("Arrears"),
        delay_interest=num("Delay interest"),
        purpose=text("Purpose"),
        birth_year=integer("Birth year"),
        family_income=num("Family income"),
        borrower_income=num("Borrower income"),
        family_liabilities=num("Family liabilities"),
        children=num("Children"),
        employment_status=_lower(text("Employment status")),
        company_age_years=num("Company age (years)"),
        diary=diary,
        parse_issues=issues,
        ambiguous_dates=ambiguous,
        missing_optional_columns=list(missing_optional),
    )


def _lower(value: str | None) -> str | None:
    return value.lower() if value is not None else None
