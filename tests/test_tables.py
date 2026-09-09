"""Long-form diary tables: every parsed diary record becomes a typed row, truncation is recorded
per loan instead of silently shortening the data, derived columns (running balance, implied
rate, days past due, buckets) agree with the rules' conventions, and the per-loan cash sums
reconcile with the summary columns on a clean loan."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from loan_dq.cli import main
from loan_dq.config import Config
from loan_dq.engine import run_pipeline
from loan_dq.ingest.model import Loan
from loan_dq.report.tables import (
    COVERAGE_COLUMNS,
    PAYMENT_COLUMNS,
    TableContext,
    coverage_row,
    diary_coverage_frame,
    payments_frame,
)
from loan_dq.report.writers import write_diary_coverage, write_payments_table
from tests.conftest import WORKBOOK, Harness, SyntheticLoan, make_frame, shift


def test_table_identity_privacy_and_nullable_types(harness: Harness, tmp_path: Path) -> None:
    synthetic = SyntheticLoan(loan_id=7)
    loan = harness.ingest_one(synthetic)
    other = harness.ingest_one(synthetic)
    other.row_index = 1
    loan.diary.payments[0].type_raw = "=private-sentinel"
    loan.diary.payments[0].loan_id_raw = "private-sentinel"
    loan.diary.parse_error = "private-sentinel"
    ctx = _ctx(harness, synthetic)
    frame = payments_frame([loan, other], ctx)
    assert set(frame["source_row_index"]) == {0, 1}
    assert "private-sentinel" not in frame.to_json()
    cover = diary_coverage_frame([loan, other], ctx)
    assert "private-sentinel" not in cover.to_json()
    empty = payments_frame([], ctx)
    for column in ("source_row_index", "loan_id", "period_no", "amount_eur", "is_late"):
        assert empty[column].dtype == frame[column].dtype
    write_payments_table([loan, other], ctx, tmp_path / "payments.csv")
    assert "private-sentinel" not in (tmp_path / "payments.csv").read_text()


def _ctx(harness: Harness, synthetic: SyntheticLoan) -> TableContext:
    return TableContext(as_of=synthetic.as_of, config=harness.config, tape_id="test")


def test_every_diary_record_becomes_one_typed_row(harness: Harness) -> None:
    synthetic = SyntheticLoan(loan_id=7)
    synthetic.rows[3].paid = shift(synthetic.rows[3].due, 12)
    synthetic.rows[3].state = "paid with delay"
    loan = harness.ingest_one(synthetic)

    frame = payments_frame([loan], _ctx(harness, synthetic))
    assert list(frame.columns) == PAYMENT_COLUMNS
    assert len(frame) == len(loan.diary.payments) == len(synthetic.rows)
    assert (frame["loan_id"] == 7).all()
    assert (frame["tape_id"] == "test").all()
    assert frame["payment_seq"].tolist() == list(range(len(synthetic.rows)))
    assert frame["record_loan_id_matches"].all()
    assert frame["parse_issues"].eq("").all()
    assert (frame["event_kind"] == "schedule").all()

    late = frame.iloc[3]
    assert late["delay_days"] == 12
    assert late["delay_bucket"] == "1-29"
    assert late["is_late"] and late["is_paid"] and not late["is_overdue"]
    assert late["state"] == "paid with delay"
    assert late["paid_eur"] == late["amount_eur"]
    assert late["due_year_month"] == late["due_date"].strftime("%Y-%m")
    assert late["actual_day_of_month"] == late["actual_date"].day

    on_time = frame[(frame["delay_days"] == 0) & frame["is_paid"]]
    assert (on_time["delay_bucket"] == "on_time").all() and not on_time["is_late"].any()

    instalments = frame.loc[frame["instalment_no"].notna(), "instalment_no"]
    assert instalments.tolist() == list(range(1, len(instalments) + 1))
    assert frame.loc[frame["type"] == "interest", "instalment_no"].isna().all()
    # principal and interest rows of the same due date share one period number
    periods = frame.groupby("due_date")["period_no"].nunique()
    assert (periods == 1).all()
    assert frame["period_no"].max() == synthetic.term


def test_running_balance_and_implied_rate_follow_the_schedule(harness: Harness) -> None:
    synthetic = SyntheticLoan(loan_id=11, amount=1200.0, annual_rate=12.0, term=12)
    loan = harness.ingest_one(synthetic)
    frame = payments_frame([loan], _ctx(harness, synthetic))

    principal = frame[frame["type"] == "principal"].sort_values("due_date")
    assert principal["reduces_principal"].all()
    assert principal["cum_principal_paid_eur"].is_monotonic_increasing
    assert principal["principal_outstanding_after_eur"].iloc[-1] == pytest.approx(0.0, abs=0.01)
    assert principal["cum_principal_paid_eur"].iloc[-1] == pytest.approx(1200.0, abs=0.01)

    interest = frame[frame["type"] == "interest"]
    assert interest["reduces_principal"].eq(False).all()
    assert interest["cum_principal_paid_eur"].isna().all()
    # each interest row is balance x 12% / 12, so the implied annual rate is ~12% on every row
    assert (interest["implied_annual_rate_pct"].sub(12.0).abs() < 0.5).all()


def test_open_rows_carry_days_past_due_against_as_of(harness: Harness) -> None:
    synthetic = SyntheticLoan(loan_id=12, paid_through=6, as_of=date(2024, 9, 1))
    loan = harness.ingest_one(synthetic)
    frame = payments_frame([loan], _ctx(harness, synthetic))

    open_rows = frame[~frame["is_paid"]]
    assert open_rows["delay_days"].isna().all()
    assert open_rows["days_past_due"].notna().all()
    overdue = open_rows[open_rows["is_overdue"]]
    future = open_rows[open_rows["is_future_scheduled"]]
    assert len(overdue) + len(future) == len(open_rows)
    assert (overdue["due_date"] < synthetic.as_of).all()
    assert (future["due_date"] > synthetic.as_of).all()
    assert (future["delay_bucket"] == "not_due").all()
    assert overdue["delay_bucket"].str.startswith("overdue_").all()
    oldest = overdue.sort_values("due_date").iloc[0]
    assert oldest["days_past_due"] == (synthetic.as_of - oldest["due_date"]).days

    cover = coverage_row(loan, _ctx(harness, synthetic))
    assert cover["overdue_open_rows"] == len(overdue)
    assert cover["max_days_past_due"] == overdue["days_past_due"].max()
    assert cover["next_due_date"] == future["due_date"].min()
    assert cover["overdue_open_eur"] == pytest.approx(overdue["pending_eur"].sum(), abs=0.01)


def test_coverage_row_reconciles_with_summary_on_clean_loan(harness: Harness) -> None:
    synthetic = SyntheticLoan(loan_id=8)
    loan = harness.ingest_one(synthetic)
    row = coverage_row(loan, _ctx(harness, synthetic))
    assert row["as_of_date"] == synthetic.as_of
    assert row["diary_readable"] and not row["diary_truncated"]
    assert row["records_parsed"] == len(synthetic.rows)
    assert row["dropped_tail_chars"] == 0
    assert row["parse_error"] is None
    assert row["open_rows"] + row["paid_rows"] == row["regular_rows"]
    assert row["periods_in_diary"] == synthetic.term
    assert row["periods_with_interest_row"] == row["periods_with_principal_row"] == synthetic.term
    assert row["periods_interest_without_principal"] == 0
    assert row["schedule_months_beyond_diary"] == 0
    assert loan.repaid_principal is not None and loan.repaid_interest is not None
    diary_principal = row["principal_paid_eur"] + row["fee_paid_eur"] + row["settlement_cash_eur"]
    assert diary_principal == pytest.approx(loan.repaid_principal, abs=0.01)
    assert row["summary_repaid_principal_eur"] == loan.repaid_principal
    assert row["principal_outstanding_end_eur"] == pytest.approx(0.0, abs=0.01)
    assert row["interest_paid_eur"] + row["overdue_interest_paid_eur"] == pytest.approx(
        loan.repaid_interest, abs=0.01
    )
    assert row["first_due_date"] == synthetic.rows[0].due
    assert row["paid_late_rows"] == 0 and row["paid_late_share"] == 0.0
    assert row["worst_delay_days"] == 0 and row["usual_due_day"] == synthetic.rows[0].due.day


def test_truncated_diary_keeps_salvaged_rows_and_records_the_cut(harness: Harness) -> None:
    synthetic = SyntheticLoan(loan_id=9)
    loan = harness.ingest_one(synthetic)
    diary_text = str(
        [r.as_record("9") for r in synthetic.rows] * 20
    )  # long enough to exceed the cell limit
    cut = diary_text[:32767]
    synthetic.summary_overrides["payments"] = cut
    truncated: Loan = harness.ingest_one(synthetic)

    assert truncated.diary.truncated and truncated.diary.readable
    ctx = _ctx(harness, synthetic)
    frame = payments_frame([truncated], ctx)
    cover = coverage_row(truncated, ctx)
    assert len(frame) == cover["records_parsed"] == cover["records_salvaged"]
    assert 0 < cover["records_parsed"] < len(synthetic.rows) * 20
    assert cover["diary_truncated"] is True
    assert cover["diary_raw_chars"] == 32767
    assert cover["dropped_tail_chars"] > 0
    # salvaged rows are complete: no half-parsed amounts or dates
    assert frame["amount_eur"].notna().all()
    assert frame["due_date"].notna().all()
    assert len(loan.diary.payments) == len(synthetic.rows)


def test_schedule_months_beyond_diary_measures_the_missing_tail(harness: Harness) -> None:
    synthetic = SyntheticLoan(loan_id=13, term=12, paid_through=6)
    # export only the first half of the schedule; the header still says the loan runs 12 months
    synthetic.rows = synthetic.rows[:12]
    loan = harness.ingest_one(synthetic)
    cover = coverage_row(loan, _ctx(harness, synthetic))
    assert cover["periods_in_diary"] == 6
    assert cover["last_due_date"] == synthetic.rows[-1].due
    assert cover["schedule_months_beyond_diary"] == 6


def test_unreadable_diary_still_gets_a_coverage_row(harness: Harness) -> None:
    synthetic = SyntheticLoan(loan_id=10)
    synthetic.summary_overrides["payments"] = "[{'Loan ID': '10', 'Payment date':"
    loan = harness.ingest_one(synthetic)
    ctx = _ctx(harness, synthetic)
    frame = diary_coverage_frame([loan], ctx)
    assert list(frame.columns) == COVERAGE_COLUMNS
    assert len(frame) == 1
    assert bool(frame.iloc[0]["diary_readable"]) is False
    assert frame.iloc[0]["parse_error"]
    assert frame.iloc[0]["loan_amount_eur"] == synthetic.amount
    assert payments_frame([loan], ctx).empty


def test_writers_emit_csv_and_cli_writes_tables(config: Config, tmp_path: Path) -> None:
    tape = tmp_path / "tape.csv"
    make_frame(SyntheticLoan(loan_id=1), SyntheticLoan(loan_id=2)).to_csv(tape, index=False)
    outcome = run_pipeline(tape, config)
    ctx = TableContext.for_tape(outcome.report.as_of, config, outcome.report.input_sha256)
    n_rows = write_payments_table(outcome.ingested.loans, ctx, tmp_path / "p.csv")
    n_loans = write_diary_coverage(outcome.ingested.loans, ctx, tmp_path / "c.csv")
    assert n_loans == 2
    assert n_rows == sum(len(loan.diary.payments) for loan in outcome.ingested.loans)
    back = pd.read_csv(tmp_path / "p.csv")
    assert list(back.columns) == PAYMENT_COLUMNS
    assert len(back) == n_rows
    assert (back["tape_id"] == outcome.report.input_sha256[:12]).all()

    out_dir = tmp_path / "out"
    assert main(["run", str(tape), "--out", str(out_dir), "--quiet"]) == 0
    assert (out_dir / "tape_payments.csv").exists()
    assert (out_dir / "tape_diary_coverage.csv").exists()
    assert main(["run", str(tape), "--out", str(out_dir / "n"), "--quiet", "--tables", "none"]) == 0
    assert not (out_dir / "n" / "tape_payments.csv").exists()
    assert (out_dir / "n" / "tape_report.json").exists()


@pytest.mark.skipif(not WORKBOOK.exists(), reason="data/loans.xlsx not present")
def test_real_tape_tables_reconcile_and_record_truncation(config: Config) -> None:
    outcome = run_pipeline(WORKBOOK, config)
    loans = outcome.ingested.loans
    ctx = TableContext.for_tape(outcome.report.as_of, config, outcome.report.input_sha256)
    payments = payments_frame(loans, ctx)
    cover = diary_coverage_frame(loans, ctx).set_index("loan_id")

    assert len(cover) == 72
    assert len(payments) == cover["records_parsed"].sum()
    assert payments["record_loan_id_matches"].all()
    assert payments["parse_issues"].eq("").all()
    assert (cover["as_of_date"] == date(2025, 1, 23)).all()

    truncated = cover[cover["diary_truncated"]]
    assert len(truncated) == 13
    assert (truncated["diary_raw_chars"] == 32767).all()
    assert (truncated["dropped_tail_chars"] > 0).all()
    assert (truncated["records_salvaged"] == truncated["records_parsed"]).all()
    # the lender writes all interest rows first, then principal/fee rows, so the cut removes
    # principal rows for whole periods: that gap is the measurable footprint of the truncation
    assert (truncated["periods_interest_without_principal"] >= 7).all()
    complete = cover[~cover["diary_truncated"]]
    assert (complete["periods_interest_without_principal"] <= 2).all()

    clean = cover.loc[99981632]
    diary_principal = (
        clean["principal_paid_eur"] + clean["fee_paid_eur"] + clean["settlement_cash_eur"]
    )
    assert diary_principal == pytest.approx(1365.0, abs=0.01)
    assert clean["closure_rows"] == 154 and clean["regular_rows"] == 25
    assert clean["principal_outstanding_end_eur"] == pytest.approx(
        clean["summary_outstanding_principal_eur"], abs=0.5
    )

    seeded = cover.loc[37216892]
    assert seeded["open_rows"] == 6 and seeded["pending_eur"] > 800
    assert seeded["max_days_past_due"] >= 90 and seeded["overdue_open_rows"] >= 1
    assert seeded["summary_days_late"] == 271
    seeded_rows = payments[payments["loan_id"] == 37216892]
    assert (seeded_rows.loc[seeded_rows["is_overdue"], "delay_bucket"] == "overdue_90+").any()
    assert json.dumps(outcome.report.to_dict(), default=str)  # tables never leak into the report
