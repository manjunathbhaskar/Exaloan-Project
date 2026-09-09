"""Plant exactly one fault in the clean loan and assert the intended rule fires as root cause.

This is the precision/recall harness: a rule that fires on the clean baseline is a false
positive, a rule that misses its planted fault is a false negative.
"""

from __future__ import annotations

from datetime import date

import pytest

from loan_dq.ingest.model import PaymentState, PaymentType
from loan_dq.report.schema import LoanResult
from loan_dq.rules.arithmetic import C2RealisedRateMatchesStated
from loan_dq.rules.base import LoanContext, Status, paid_portion, pending_portion
from loan_dq.rules.xirr import xirr
from tests.conftest import DiaryRow, Harness, SyntheticLoan, failed_ids, shift


def roots(result: LoanResult) -> set[str]:
    return {f.rule_id for f in result.findings if f.root_cause and f.severity != "info"}


def finding(result: LoanResult, rule_id: str) -> str:
    return next(f.message for f in result.findings if f.rule_id == rule_id)


# --- F: lateness -----------------------------------------------------------------------------


@pytest.mark.parametrize(("delay", "expect"), [(89, "F3"), (90, "F1"), (91, "F1")])
def test_paid_late_boundary(harness: Harness, delay: int, expect: str) -> None:
    loan = SyntheticLoan()
    row = loan.rows[6]  # 4th instalment, principal
    row.paid = shift(row.due, delay)
    row.state = "paid with delay"
    result = harness.evaluate(loan)
    assert expect in roots(result), result.findings
    if expect == "F1":
        assert "F3" not in roots(result)
        assert result.credit_event == "default"
        assert f"{delay} days after scheduled date" in finding(result, "F1")
    else:
        assert result.credit_event == "watch"
    assert result.data_integrity == "clean"


@pytest.mark.parametrize(
    ("delay", "severity", "verdict"),
    [(30, "low", "normal"), (59, "low", "normal"), (60, "medium", "flagged")],
)
def test_watch_band_paid_delay_severity(
    harness: Harness, delay: int, severity: str, verdict: str
) -> None:
    loan = SyntheticLoan()
    row = loan.rows[6]
    row.paid = shift(row.due, delay)
    row.state = "paid with delay"
    result = harness.evaluate(loan)
    f3 = next(f for f in result.findings if f.rule_id == "F3")
    assert f3.severity == severity
    assert result.verdict == verdict
    assert result.credit_event == "watch"
    if verdict == "normal":
        assert result.primary_reason.startswith("no major issues; minor observation:")
        assert f3.message in result.primary_reason


def test_watch_band_still_unpaid_is_medium(harness: Harness) -> None:
    loan = SyntheticLoan(paid_through=6)
    unpaid_due = loan.rows[12].due
    loan.summary_overrides["Days late"] = 40
    result = harness.evaluate(loan, as_of=shift(unpaid_due, 40))
    f3 = next(f for f in result.findings if f.rule_id == "F3")
    assert f3.severity == "medium"
    assert result.verdict == "flagged"


def test_paid_late_message_names_both_dates(harness: Harness) -> None:
    loan = SyntheticLoan()
    row = loan.rows[6]
    row.paid = shift(row.due, 92)
    row.state = "paid with delay"
    msg = finding(harness.evaluate(loan), "F1")
    assert row.due.isoformat() in msg and row.paid.isoformat() in msg
    assert "92 days" in msg


@pytest.mark.parametrize(("days_past_due", "expect"), [(89, "F3"), (90, "F2"), (120, "F2")])
def test_unpaid_overdue_boundary(harness: Harness, days_past_due: int, expect: str) -> None:
    loan = SyntheticLoan(paid_through=6)
    unpaid_due = loan.rows[12].due  # 7th instalment
    as_of = shift(unpaid_due, days_past_due)
    loan.summary_overrides["Days late"] = days_past_due
    result = harness.evaluate(loan, as_of=as_of)
    assert expect in roots(result), result.findings
    if expect == "F2":
        assert result.severity == "critical"
        assert result.credit_event == "default"
        assert f"{days_past_due} days overdue" in finding(result, "F2")


def test_overdue_amount_is_the_pending_remainder_not_the_gross_row(harness: Harness) -> None:
    loan = SyntheticLoan(paid_through=6)
    principal, interest = loan.rows[12], loan.rows[13]  # 7th instalment
    principal.pending = round(principal.amount / 2, 2)  # part-paid
    as_of = shift(principal.due, 100)
    loan.summary_overrides["Days late"] = 100
    result = harness.evaluate(loan, as_of=as_of)
    f2 = next(f for f in result.findings if f.rule_id == "F2")
    expected = round(principal.pending + interest.amount, 2)
    assert f2.evidence["overdue_amount"] == expected
    assert f2.evidence["components_overdue"] == 2
    assert f2.evidence["due_dates_overdue"] == 1
    assert f"2 payment component(s) across 1 due date(s) with EUR {expected:,.2f} still unpaid" in (
        f2.message
    )


def test_overdue_message_counts_match_the_selected_rows(harness: Harness) -> None:
    # Two instalments (principal + interest each) are 90+ days past due, a third is only 40 days:
    # every count and amount in the message must be derivable from exactly those four rows.
    loan = SyntheticLoan(paid_through=5)
    as_of = shift(loan.rows[12].due, 100)
    loan.rows[10].pending = round(loan.rows[10].amount * 0.25, 2)  # part-paid principal
    loan.summary_overrides["Days late"] = (as_of - loan.rows[10].due).days
    selected = [r for r in loan.rows if not r.paid and (as_of - r.due).days >= 90]
    assert len(selected) == 4 and len({r.due for r in selected}) == 2
    expected_amount = round(sum(r.pending for r in selected), 2)
    assert expected_amount < round(sum(r.amount for r in selected), 2)
    result = harness.evaluate(loan, as_of=as_of)
    f2 = next(f for f in result.findings if f.rule_id == "F2")
    assert f2.evidence["components_overdue"] == 4
    assert f2.evidence["due_dates_overdue"] == 2
    assert f2.evidence["overdue_amount"] == expected_amount
    assert f2.message.startswith(
        f"4 payment component(s) across 2 due date(s) with EUR {expected_amount:,.2f} still unpaid"
    )
    assert "instalment(s)" not in f2.message


def test_unpaid_but_not_yet_due_is_normal(harness: Harness) -> None:
    loan = SyntheticLoan(paid_through=6)
    result = harness.evaluate(loan, as_of=shift(loan.rows[10].due, 3))
    assert failed_ids(result) == set()


def test_early_payment_before_due_is_not_late(harness: Harness) -> None:
    loan = SyntheticLoan()
    row = loan.rows[6]
    row.paid = shift(row.due, -3)
    result = harness.evaluate(loan)
    assert failed_ids(result) == set()


# --- B: summary vs diary identities -----------------------------------------------------------


def test_days_late_contradicts_diary(harness: Harness) -> None:
    loan = SyntheticLoan(paid_through=6)
    unpaid_due = loan.rows[12].due
    as_of = shift(unpaid_due, 100)
    loan.summary_overrides["Days late"] = 0
    result = harness.evaluate(loan, as_of=as_of)
    assert "B7" in failed_ids(result)
    assert "F2" in roots(result)


def test_repaid_principal_mismatch(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Repaid principal"] = 1150.0
    result = harness.evaluate(loan)
    assert {"B2", "B3"} <= roots(result)
    assert result.data_integrity == "defect"
    assert "1,150.00" in finding(result, "B3")


def test_repaid_with_outstanding_balance_is_one_root_cause(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Repaid principal"] = 1150.0
    loan.summary_overrides["Outstanding principal"] = 50.0
    result = harness.evaluate(loan)
    assert roots(result) == {"E1"}
    symptom = next(f for f in result.findings if f.rule_id == "B3")
    assert symptom.root_cause is False and symptom.symptom_of == "E1"


def test_outstanding_plus_repaid_not_loan_amount(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Outstanding principal"] = 10.0
    result = harness.evaluate(loan)
    assert "B2" in failed_ids(result)


def test_principal_tolerance_absorbs_integer_rounding(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Loan amount"] = 1200.0
    loan.summary_overrides["Repaid principal"] = 1200.47
    result = harness.evaluate(loan)
    assert "B2" not in failed_ids(result)
    assert "B3" not in failed_ids(result)


def test_repaid_interest_mismatch(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Repaid interest"] = 5.0
    result = harness.evaluate(loan)
    assert "B4" in roots(result)


def test_negative_outstanding_principal(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Outstanding principal"] = -12.5
    result = harness.evaluate(loan)
    assert "C4" in roots(result)
    assert "negative" in finding(result, "C4").lower()
    assert "Outstanding principal" in finding(result, "C4")


# --- C: arithmetic and XIRR ---------------------------------------------------------------------


def test_zero_interest_paid_at_stated_rate_fires_xirr(harness: Harness) -> None:
    loan = SyntheticLoan(annual_rate=25.0)
    for row in loan.rows:
        if row.type == "interest":
            row.amount = 0.0
    loan.summary_overrides["Repaid interest"] = 0.0
    result = harness.evaluate(loan)
    assert "C2" in roots(result)
    assert "25%" in finding(result, "C2")
    assert result.severity in {"high", "critical"}


def _deferred_annuity(*, interest_due: date) -> SyntheticLoan:
    """Deferred annuity at 25 % settled in three monthly principal payments with no interest
    paid; its single scheduled interest instalment falls due at ``interest_due``."""
    loan = SyntheticLoan(annual_rate=25.0, term=12)
    dues = [loan.rows[2 * k].due for k in range(3)]
    settled = dues[-1]
    loan.rows = [
        DiaryRow(d, d, "principal", "paid on time", round(loan.amount / 3, 2)) for d in dues
    ]
    loan.rows.append(DiaryRow(interest_due, settled, "interest", "paid on time", 0.0))
    loan.summary_overrides.update(
        {
            "Loan type": "deferred annuity",
            "Loan status": "repaid",
            "Repaid principal": loan.amount,
            "Outstanding principal": 0.0,
            "Repaid interest": 0.0,
            "Outstanding interest": 0.0,
            "Repayment date": settled.isoformat() + "T00:00:00.000",
        }
    )
    return loan


def test_deferred_annuity_settled_in_interest_holiday_is_a_low_confidence_review(
    harness: Harness,
) -> None:
    # No interest fell due before settlement: the mismatch is kept as a finding (no product
    # documentation says early settlement waives deferred interest) but marked low confidence,
    # capped at medium, and worded as a review item rather than a verdict.
    holiday_ends = shift(SyntheticLoan().disbursed, 330)
    result = harness.evaluate(_deferred_annuity(interest_due=holiday_ends))
    c2 = next(f for f in result.findings if f.rule_id == "C2")
    assert c2.severity == "low"
    assert c2.evidence["confidence"] == "low"
    assert "before any scheduled interest" in str(c2.evidence["confidence_reason"])
    assert c2.message.startswith("stated rate 25% versus realised")
    assert "inconsistent" not in c2.message
    assert "not a confirmed rate defect" in c2.message
    assert "review against early-settlement/deferred-annuity terms" in c2.message


def test_deferred_annuity_settled_after_interest_fell_due_is_measured_with_confidence(
    harness: Harness,
) -> None:
    # Interest fell due with the first instalment: interest was realisable, none was paid. The
    # loan still settled before its expected repayment date, so confidence is medium, not low.
    loan = _deferred_annuity(interest_due=SyntheticLoan().rows[0].due)
    result = harness.evaluate(loan)
    c2 = next(f for f in result.findings if f.rule_id == "C2")
    assert c2.evidence["confidence"] == "medium"
    assert c2.severity == "high"


def test_plain_instalment_rate_mismatch_is_high_confidence(harness: Harness) -> None:
    loan = SyntheticLoan(annual_rate=25.0)
    for row in loan.rows:
        if row.type == "interest":
            row.amount = 0.0
    loan.summary_overrides["Repaid interest"] = 0.0
    c2 = next(f for f in harness.evaluate(loan).findings if f.rule_id == "C2")
    assert c2.evidence["confidence"] == "high"
    assert "inconsistent with the stated rate" in c2.message
    assert "review against" not in c2.message


# --- C10: conflicting rows in one schedule slot ---------------------------------------------


def test_second_fee_row_for_same_due_date_with_different_amount_is_c10(
    harness: Harness,
) -> None:
    loan = SyntheticLoan()
    due = loan.rows[6].due
    loan.rows.append(DiaryRow(due, due, "contract fee repayment", "paid on time", 2.07))
    loan.rows.append(DiaryRow(due, shift(due, 1), "contract fee repayment", "paid on time", 2.65))
    result = harness.evaluate(loan)
    # Fees amortise as principal on this tape, so B1 also notices the EUR 4.72 the summary lacks.
    assert roots(result) == {"C10", "B1"}, result.findings
    c10 = next(f for f in result.findings if f.rule_id == "C10")
    assert c10.severity == "low" and c10.root_cause
    slots = c10.evidence["slots"]
    assert isinstance(slots, list) and len(slots) == 1
    assert slots[0]["type"] == "contract fee repayment"
    assert slots[0]["due_date"] == due.isoformat()
    assert slots[0]["differs_in"] == ["amount", "actual_date"]
    assert "EUR 2.07 / EUR 2.65" in c10.message
    # The rule fired on the slot, not on the byte-identical-duplicate path.
    assert "C9" not in failed_ids(result)


def test_paid_and_unpaid_twin_in_one_slot_is_a_medium_c10(harness: Harness) -> None:
    loan = SyntheticLoan(paid_through=6)
    twin = loan.rows[12]  # 7th principal, pending
    loan.rows.append(DiaryRow(twin.due, twin.due, twin.type, "paid on time", twin.amount))
    result = harness.evaluate(loan, as_of=shift(twin.due, 3))
    c10 = next(f for f in result.findings if f.rule_id == "C10")
    assert c10.severity == "medium" and result.verdict == "flagged"
    assert "state" in c10.evidence["slots"][0]["differs_in"]


def test_byte_identical_duplicate_is_c9_not_c10(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.rows.append(loan.rows[6])
    result = harness.evaluate(loan)
    assert "C9" in failed_ids(result)
    assert "C10" not in failed_ids(result)


def test_settlement_and_closure_rows_do_not_trigger_c10(harness: Harness) -> None:
    # Two full-early-repayment rows on the same date (an aborted attempt and the real one) and
    # a same-day zero closure row are lender conventions, not conflicting schedule rows.
    loan = SyntheticLoan(paid_through=6)
    settle = shift(loan.rows[12].due, -10)
    remaining = round(sum(r.amount for r in loan.rows if r.type == "principal" and not r.paid), 2)
    loan.rows = [r for r in loan.rows if r.paid]
    loan.rows.append(
        DiaryRow(settle, None, "full early repayment", "pending", remaining, remaining)
    )
    loan.rows.append(DiaryRow(settle, settle, "full early repayment", "paid on time", remaining))
    loan.rows.append(DiaryRow(settle, settle, "principal", "paid on time", 0.0))
    loan.rows.append(DiaryRow(settle, settle, "interest", "paid on time", 0.0))
    loan.summary_overrides.update(
        {
            "Loan status": "repaid",
            "Repaid principal": loan.amount,
            "Outstanding principal": 0.0,
            "Outstanding interest": 0.0,
            "Repayment date": settle.isoformat() + "T00:00:00.000",
        }
    )
    result = harness.evaluate(loan)
    assert "C10" not in failed_ids(result), [f.message for f in result.findings]


def test_c10_is_not_evaluable_on_an_empty_diary(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["payments"] = "[]"
    result = harness.evaluate(loan)
    c10 = next(o for o in result.rule_outcomes if o.rule_id == "C10")
    assert c10.status == "not_evaluable"
    assert "C10" not in failed_ids(result)


def test_interest_instalment_inconsistent_with_rate(harness: Harness) -> None:
    loan = SyntheticLoan()
    for row in loan.rows:
        if row.type == "interest":
            row.amount = round(row.amount * 3, 2)
    loan.summary_overrides["Repaid interest"] = round(
        sum(r.amount for r in loan.rows if r.type == "interest"), 2
    )
    result = harness.evaluate(loan)
    assert {"C1", "C2"} & failed_ids(result)


def test_paid_row_with_pending_amount(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.rows[4].pending = 25.0
    result = harness.evaluate(loan)
    assert "C6" in roots(result)


def test_negative_diary_amount(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.rows[4].amount = -loan.rows[4].amount
    result = harness.evaluate(loan)
    assert "C4" in roots(result)


# --- D: temporal ---------------------------------------------------------------------------------


def test_payment_before_disbursal(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.rows[0].paid = shift(loan.disbursed, -10)
    result = harness.evaluate(loan)
    assert "D1" in failed_ids(result)


def test_future_dated_payment(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.rows[-1].paid = date(2030, 1, 1)
    result = harness.evaluate(loan)
    assert "D2" in failed_ids(result)


@pytest.mark.parametrize(("delay", "fires"), [(5, False), (6, True)])
def test_paid_on_time_label_beyond_grace(harness: Harness, delay: int, fires: bool) -> None:
    loan = SyntheticLoan()
    for row in loan.rows[6:8]:  # 4th instalment, both rows keep the lender's 'paid on time'
        row.paid = shift(row.due, delay)
    result = harness.evaluate(loan)
    if not fires:
        assert "D8" not in failed_ids(result)
        return
    d8 = next(f for f in result.findings if f.rule_id == "D8")
    assert d8.severity == "low" and d8.evidence["rows"] == 2
    assert f"{delay} days" in d8.message
    # A mislabelled row is a data observation, not a credit event.
    assert result.verdict == "normal" and result.credit_event == "none"


def test_schedule_gap(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.rows = [r for r in loan.rows if r.due != loan.rows[10].due]
    loan.summary_overrides["Repaid principal"] = round(
        sum(r.amount for r in loan.rows if r.type == "principal"), 2
    )
    loan.summary_overrides["Repaid interest"] = round(
        sum(r.amount for r in loan.rows if r.type == "interest"), 2
    )
    result = harness.evaluate(loan)
    assert "D3" in failed_ids(result)


# --- E: lifecycle ---------------------------------------------------------------------------------


def test_repaid_status_with_unpaid_rows(harness: Harness) -> None:
    loan = SyntheticLoan(paid_through=10)
    loan.summary_overrides["Loan status"] = "repaid"
    loan.summary_overrides["Repayment date"] = "2025-01-15T00:00:00.000"
    result = harness.evaluate(loan)
    assert "E1" in roots(result)
    assert "repaid" in finding(result, "E1")


def test_paid_state_without_repayment_date(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.rows[4].paid = None
    result = harness.evaluate(loan)
    assert "E4" in failed_ids(result)


def test_pending_state_with_repayment_date(harness: Harness) -> None:
    loan = SyntheticLoan(paid_through=6, as_of=date(2024, 7, 20))
    row = loan.rows[14]
    row.paid = row.due
    result = harness.evaluate(loan)
    assert "E4" in failed_ids(result)


def test_live_loan_with_nothing_outstanding(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Loan status"] = "granted"
    loan.summary_overrides["Repayment date"] = None
    result = harness.evaluate(loan)
    assert "E2" in failed_ids(result)


def test_terminated_unresolved_amount_is_pending_remainder_of_the_selected_rows(
    harness: Harness,
) -> None:
    # Terminated loan: two unpaid instalments (one principal row part-paid) plus a pending
    # termination claim. The E3 message must sum pending remainders, not gross rows, and count
    # component rows and distinct due dates separately.
    loan = SyntheticLoan(paid_through=6)
    open_rows = [r for r in loan.rows if not r.paid]
    open_rows[0].pending = round(open_rows[0].amount * 0.4, 2)  # 7th principal, part-paid
    loan.rows = [r for r in loan.rows if r.paid] + open_rows[:4]  # keep 7th and 8th instalments
    claim = round(sum(r.amount for r in open_rows[4:] if r.type == "principal"), 2)
    claim_due = open_rows[4].due
    loan.rows.append(
        DiaryRow(claim_due, None, "repayment after agreement termination", "pending", claim, claim)
    )
    loan.summary_overrides.update({"Loan status": "terminated", "Days late": 100})
    unpaid = open_rows[:4]
    expected_unpaid = round(sum(r.pending for r in unpaid), 2)
    expected_total = round(expected_unpaid + claim, 2)
    assert expected_unpaid < round(sum(r.amount for r in unpaid), 2)
    result = harness.evaluate(loan, as_of=shift(unpaid[0].due, 100))
    e3 = next(f for f in result.findings if f.rule_id == "E3")
    assert e3.evidence["unresolved_amount"] == expected_total
    assert e3.evidence["unpaid_components"] == 4
    assert e3.evidence["unpaid_due_dates"] == 2
    assert e3.evidence["unpaid_amount"] == expected_unpaid
    assert e3.evidence["pending_termination_rows"] == 1
    assert e3.evidence["pending_termination_amount"] == claim
    assert e3.message == (
        f"loan is terminated with EUR {expected_total:,.2f} unresolved: 4 unpaid payment "
        f"component(s) across 2 due date(s) totalling EUR {expected_unpaid:,.2f} plus 1 pending "
        f"termination claim(s) of EUR {claim:,.2f}"
    )


# --- G: behaviour drift -----------------------------------------------------------------------


def _delay_last_instalments(loan: SyntheticLoan, delays: list[int]) -> None:
    """Pay the last ``len(delays)`` instalments late by the given days (all components)."""
    dues = sorted({r.due for r in loan.rows})[-len(delays) :]
    for due, days in zip(dues, delays, strict=True):
        for r in loan.rows:
            if r.due == due:
                r.paid = shift(due, days)
                r.state = "paid with delay" if days > 5 else "paid on time"


def test_stable_payer_drifting_late_is_g8(harness: Harness) -> None:
    loan = SyntheticLoan()  # 12 instalments, all paid on the due date
    _delay_last_instalments(loan, [7, 2, 9])
    result = harness.evaluate(loan)
    assert "G8" in failed_ids(result), [f.message for f in result.findings]
    g8 = next(f for f in result.findings if f.rule_id == "G8")
    assert g8.severity == "low" and g8.axis == "credit"
    assert result.verdict == "normal" and result.credit_event == "none"
    assert g8.evidence["baseline_episodes"] == 9
    assert g8.evidence["baseline_worst_days"] == 0
    assert g8.evidence["recent_delays_days"] == [7, 2, 9]
    assert g8.evidence["recent_exceeding"] == 2
    assert "9 earlier payment episodes" in g8.message and "2 of the last 3" in g8.message


def test_components_of_one_late_instalment_count_as_one_episode(harness: Harness) -> None:
    # Principal 7 d, interest 8 d and a fee 9 d late all belong to the same due date: one
    # episode, not three, so a single slip never looks like a repeated pattern.
    loan = SyntheticLoan()
    last = loan.rows[-2].due
    loan.rows.append(
        DiaryRow(last, shift(last, 9), "contract fee repayment", "paid with delay", 1.5)
    )
    loan.rows[-3].paid, loan.rows[-3].state = shift(last, 7), "paid with delay"
    loan.rows[-2].paid, loan.rows[-2].state = shift(last, 8), "paid with delay"
    result = harness.evaluate(loan)
    assert "G8" not in failed_ids(result)
    g8 = next(o for o in result.rule_outcomes if o.rule_id == "G8")
    assert g8.status == "pass"


def test_single_recent_slip_is_not_drift(harness: Harness) -> None:
    loan = SyntheticLoan()
    _delay_last_instalments(loan, [0, 7, 1])
    result = harness.evaluate(loan)
    assert next(o for o in result.rule_outcomes if o.rule_id == "G8").status == "pass"


def test_irregular_baseline_is_not_evaluable_for_drift(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.rows[2].paid = shift(loan.rows[2].due, 12)  # 2nd instalment already 12 d late
    loan.rows[2].state = loan.rows[3].state = "paid with delay"
    loan.rows[3].paid = loan.rows[2].paid
    _delay_last_instalments(loan, [7, 8, 9])
    result = harness.evaluate(loan)
    g8 = next(o for o in result.rule_outcomes if o.rule_id == "G8")
    assert g8.status == "not_evaluable" and "baseline already irregular" in g8.note


def test_too_few_episodes_is_not_evaluable_for_drift(harness: Harness) -> None:
    loan = SyntheticLoan(paid_through=5)
    result = harness.evaluate(loan, as_of=shift(loan.rows[10].due, 3))
    g8 = next(o for o in result.rule_outcomes if o.rule_id == "G8")
    assert g8.status == "not_evaluable" and "need at least 6" in g8.note


# --- A / H: readability and plausibility ---------------------------------------------------------


def test_unknown_payment_state(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.rows[3].state = "definitely maybe"
    result = harness.evaluate(loan)
    assert "A9" in failed_ids(result)
    assert "unknown payment state" in finding(result, "A9")
    assert "definitely maybe" not in finding(result, "A9")


def test_untranslated_purpose_code_is_low_and_not_flagged(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Purpose"] = "loan_purpose.12"
    result = harness.evaluate(loan)
    assert "A6" in failed_ids(result)
    assert result.severity == "low"
    assert result.verdict == "normal"
    assert result.data_integrity == "clean"
    assert "untranslated" in result.primary_reason


def test_borrower_age_out_of_range(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Birth year"] = 2015.0
    result = harness.evaluate(loan)
    assert "H1" in failed_ids(result)
    assert "2015" not in finding(result, "H1")


def test_payment_exceeds_income(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Borrower income"] = 50.0
    loan.summary_overrides["Family income"] = 50.0
    result = harness.evaluate(loan)
    assert "H3" in failed_ids(result)


def test_interest_rate_out_of_product_range(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Interest rate"] = 250.0
    result = harness.evaluate(loan)
    assert "H5" in failed_ids(result)


@pytest.mark.parametrize("state", [PaymentState.PAID_ON_TIME, PaymentState.PENDING])
@pytest.mark.parametrize("pending", [0.0, 25.0, 100.0])
def test_cash_and_exposure_follow_sane_remainder(
    harness: Harness, state: PaymentState, pending: float
) -> None:
    parsed = harness.ingest_one(SyntheticLoan())
    row = parsed.diary.payments[0]
    row.amount, row.pending_amount, row.state = 100.0, pending, state
    assert paid_portion(row) == 100.0 - pending
    assert pending_portion(row) == pending
    assert paid_portion(row) + pending_portion(row) == row.amount


def test_pending_zero_settlement_marker_never_becomes_cash(harness: Harness) -> None:
    parsed = harness.ingest_one(SyntheticLoan())
    row = parsed.diary.payments[0]
    row.type, row.state = PaymentType.FULL_EARLY, PaymentState.PENDING
    row.pending_amount, row.is_settlement_marker = 0.0, True
    assert paid_portion(row) == 0.0
    assert pending_portion(row) == 0.0


def test_paid_label_does_not_hide_open_exposure_or_repaid_contradiction(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.rows[4].pending = 25.0
    result = harness.evaluate(loan)
    assert {"C6", "E1", "F2"} <= failed_ids(result)
    e1 = next(f for f in result.findings if f.rule_id == "E1")
    f2 = next(f for f in result.findings if f.rule_id == "F2")
    assert e1.evidence["unpaid_amount"] == f2.evidence["overdue_amount"] == 25.0


def test_overdue_interest_identity_uses_partial_receipt_despite_paid_label(
    harness: Harness,
) -> None:
    loan = SyntheticLoan()
    due = loan.rows[6].due
    loan.rows.append(DiaryRow(due, due, "overdue interest", "paid with delay", 92.31, 55.75))
    loan.summary_overrides["Delay interest"] = 36.56
    result = harness.evaluate(loan)
    assert "B6" not in failed_ids(result)
    assert "C6" in failed_ids(result)


@pytest.mark.parametrize("state", ["paid on time", "pending"])
def test_xirr_skips_receipts_without_actual_dates(harness: Harness, state: str) -> None:
    loan = SyntheticLoan()
    loan.rows[4].pending, loan.rows[4].paid, loan.rows[4].state = 25.0, None, state
    result = harness.evaluate(loan)
    c2 = next(o for o in result.rule_outcomes if o.rule_id == "C2")
    assert c2.status == "not_evaluable"
    assert "undated" in c2.note and "receipt" in c2.note
    assert result.validation_coverage == "partial"


@pytest.mark.parametrize("state", ["paid on time", "pending"])
def test_xirr_dated_partial_receipt_uses_net_amount(harness: Harness, state: str) -> None:
    loan = SyntheticLoan()
    loan.rows[4].pending = 25.0
    loan.rows[4].state = state
    parsed = harness.ingest_one(loan)
    outcome = C2RealisedRateMatchesStated().evaluate(
        LoanContext(parsed, harness.config, loan.as_of)
    )
    expected = xirr(
        [(loan.disbursed, -loan.amount)]
        + [(r.paid, r.amount - r.pending) for r in loan.rows if r.paid]
    )
    assert expected is not None and outcome.status is not Status.NOT_EVALUABLE
    assert outcome.evidence["realised_xirr_pct"] == round(expected * 100, 2)
    result = harness.evaluate(loan)
    assert "E4" not in failed_ids(result)
    if state == "pending":
        assert "no repayment date recorded" not in finding(result, "C6")


@pytest.mark.parametrize("interest_factor", [1.0, 0.25])
def test_xirr_lateness_suppression_requires_counterfactual_evidence(
    harness: Harness, interest_factor: float
) -> None:
    loan = SyntheticLoan(annual_rate=25.0)
    for row in loan.rows:
        row.paid, row.state = shift(row.due, 365), "paid with delay"
        if row.type == "interest":
            row.amount = round(row.amount * interest_factor, 2)
    result = harness.evaluate(loan, as_of=shift(loan.rows[-1].due, 366))
    c2 = next(f for f in result.findings if f.rule_id == "C2")
    assert "F1" in roots(result)
    assert c2.root_cause is (interest_factor != 1.0)
    assert c2.evidence["timing_explains_gap"] is (interest_factor == 1.0)


def test_principal_shortfall_does_not_confirm_an_independent_rate_defect(harness: Harness) -> None:
    loan = SyntheticLoan(annual_rate=25.0)
    loan.rows[4].amount -= 75.0
    loan.summary_overrides["Repaid principal"] = loan.amount
    result = harness.evaluate(loan)
    c2 = next(f for f in result.findings if f.rule_id == "C2")
    assert c2.severity == "low" and c2.symptom_of == "B1"
    assert "principal shortfall" in c2.message and "not a confirmed rate defect" in c2.message


@pytest.mark.parametrize("rule_id", ["H3", "H10"])
def test_income_employment_signals_are_contextual_review(harness: Harness, rule_id: str) -> None:
    loan = SyntheticLoan()
    if rule_id == "H3":
        loan.summary_overrides.update({"Borrower income": 50.0, "Family income": 50.0})
    else:
        loan.summary_overrides["Employment status"] = "unemployed"
    result = harness.evaluate(loan)
    signal = next(f for f in result.findings if f.rule_id == rule_id)
    assert result.data_integrity == "clean" and result.verdict == "normal"
    assert signal.severity == "low" and "review" in signal.message


@pytest.mark.parametrize("endpoint", [1, -1])
def test_c1_does_not_flag_normal_first_or_last_accrual_stub(
    harness: Harness, endpoint: int
) -> None:
    loan = SyntheticLoan()
    loan.rows[endpoint].amount *= 0.25
    result = harness.evaluate(loan)
    assert "C1" not in failed_ids(result)


def test_isolated_future_interest_spike_remains_independent_of_historical_lateness(
    harness: Harness,
) -> None:
    loan = SyntheticLoan(paid_through=6)
    loan.rows[0].paid, loan.rows[0].state = shift(loan.rows[0].due, 90), "paid with delay"
    loan.rows[15].amount += 100.0
    loan.rows[15].pending += 100.0
    result = harness.evaluate(loan, as_of=shift(loan.rows[10].due, 3))
    c1 = next(f for f in result.findings if f.rule_id == "C1")
    assert c1.root_cause and "isolated" in c1.message
    assert "F1" in roots(result)


def test_long_first_accrual_is_not_an_isolated_material_fault(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.disbursed = shift(loan.disbursed, -30)
    first = loan.rows[1]
    days = (first.due - loan.disbursed).days
    first.amount = round(loan.amount * loan.annual_rate / 100.0 * days / 360.0, 2)
    assert first.amount > 2 * loan.rows[3].amount
    assert "C1" not in failed_ids(harness.evaluate(loan))
