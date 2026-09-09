"""The synthetic clean loan must pass every rule - it is the baseline for fault injection."""

from __future__ import annotations

from datetime import date

from tests.conftest import Harness, SyntheticLoan, failed_ids


def test_repaid_clean_loan_is_normal(harness: Harness) -> None:
    result = harness.evaluate(SyntheticLoan())
    assert failed_ids(result) == set(), [f.message for f in result.findings]
    assert result.verdict == "normal"
    assert result.severity is None
    assert result.data_integrity == "clean"
    assert result.credit_event == "none"
    assert result.validation_coverage == "full"
    assert result.checks_failed == 0
    assert result.checks_passed > 20


def test_live_clean_loan_is_normal(harness: Harness) -> None:
    loan = SyntheticLoan(paid_through=6, as_of=date(2024, 7, 20))
    result = harness.evaluate(loan)
    assert failed_ids(result) == set(), [f.message for f in result.findings]
    assert result.verdict == "normal"
    assert result.loan_status == "granted"


def test_clean_loan_has_behaviour_profile(harness: Harness) -> None:
    result = harness.evaluate(SyntheticLoan())
    profile = result.behaviour_profile
    assert profile is not None
    assert profile["instalments_paid"] == 24  # 12 principal + 12 interest rows
    assert profile["paid_late_count"] == 0
    assert profile["worst_delay_days"] == 0
