"""Engine mechanics: rule isolation, three-valued outcomes, root-cause de-dup, two axes,
as-of resolution, tape circuit breaker, determinism."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from loan_dq.config import Config
from loan_dq.engine import evaluate_loan, run
from loan_dq.report.schema import LoanResult, RuleOutcome, loan_tier
from loan_dq.rules.base import Axis, BaseRule, LoanContext, Outcome, Severity, Status
from tests.conftest import Harness, SyntheticLoan, failed_ids, make_frame, shift


def outcome_of(result: LoanResult, rule_id: str) -> RuleOutcome:
    return next(o for o in result.rule_outcomes if o.rule_id == rule_id)


class ExplodingRule(BaseRule):
    id: ClassVar[str] = "Z1"
    category: ClassVar[str] = "test"
    severity: ClassVar[Severity] = Severity.CRITICAL
    axis: ClassVar[Axis] = Axis.INTEGRITY
    title: ClassVar[str] = "always raises"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        raise ZeroDivisionError("planted")


class SkippingRule(BaseRule):
    id: ClassVar[str] = "Z2"
    category: ClassVar[str] = "test"
    severity: ClassVar[Severity] = Severity.CRITICAL
    axis: ClassVar[Axis] = Axis.INTEGRITY
    title: ClassVar[str] = "always not evaluable"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        return Outcome.skip("planted skip")


def test_rule_exception_is_isolated_and_recorded(harness: Harness) -> None:
    loan = harness.ingest_one(SyntheticLoan())
    rules = [*harness.rules, ExplodingRule()]
    result = evaluate_loan(loan, harness.config, date(2025, 2, 1), rules)
    assert result.verdict == "normal"
    outcome = outcome_of(result, "Z1")
    assert outcome.status == Status.NOT_EVALUABLE.value
    assert "ZeroDivisionError" in outcome.note
    assert result.checks_not_evaluable >= 1
    assert result.validation_coverage == "partial"


def test_not_evaluable_is_never_a_finding(harness: Harness) -> None:
    loan = harness.ingest_one(SyntheticLoan())
    result = evaluate_loan(loan, harness.config, date(2025, 2, 1), [*harness.rules, SkippingRule()])
    assert result.verdict == "normal"
    assert result.severity is None
    assert outcome_of(result, "Z2").note == "planted skip"
    assert "Z2" not in {f.rule_id for f in result.findings}


def test_truncated_diary_yields_partial_coverage_not_flags(harness: Harness) -> None:
    loan = SyntheticLoan(term=120, paid_through=30, as_of=date(2026, 8, 1))
    text = str([r.as_record(str(loan.loan_id)) for r in loan.rows])
    assert len(text) > 32767, "fixture must exceed Excel's cell limit"
    loan.summary_overrides["payments"] = text[:32767]
    result = harness.evaluate(loan)
    assert result.validation_coverage == "partial"
    assert result.data_integrity == "unknown"
    assert failed_ids(result) == set(), [f.message for f in result.findings]
    a8 = next(f for f in result.findings if f.rule_id == "A8")
    assert a8.severity == "info" and a8.axis == "coverage"
    assert result.verdict == "normal"
    # Complete-history identities are skipped, not failed, on a truncated diary.
    assert outcome_of(result, "B1").status == Status.NOT_EVALUABLE.value
    # ...and the summary must not count 'no finding on half a diary' as a clean loan.
    assert loan_tier(result) == "indeterminate"
    assert loan_tier(harness.evaluate(SyntheticLoan())) == "clean"


def test_two_axes_are_independent(harness: Harness) -> None:
    # Honest 100-day late payment: credit event, integrity clean.
    loan = SyntheticLoan()
    row = loan.rows[6]
    row.paid = shift(row.due, 100)
    row.state = "paid with delay"
    honest = harness.evaluate(loan)
    assert honest.credit_event == "default" and honest.data_integrity == "clean"

    # Summary contradicts diary: integrity defect, no credit event.
    loan = SyntheticLoan()
    loan.summary_overrides["Repaid interest"] = 1.0
    dishonest = harness.evaluate(loan)
    assert dishonest.data_integrity == "defect" and dishonest.credit_event == "none"


def test_primary_reason_is_root_cause_message(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Repaid principal"] = 1150.0
    loan.summary_overrides["Outstanding principal"] = 50.0
    result = harness.evaluate(loan)
    e1 = next(f for f in result.findings if f.rule_id == "E1")
    assert result.primary_reason == e1.message


def test_verdict_floor_is_configurable(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Purpose"] = "loan_purpose.12"  # A6, low
    default = harness.evaluate(loan)
    assert default.verdict == "normal" and default.severity == "low"
    assert default.data_integrity == "clean"

    strict = Config.model_validate(
        {**harness.config.model_dump(), "verdict": {"flag_min_severity": "low"}}
    )
    parsed = harness.ingest_one(loan)
    result = evaluate_loan(parsed, strict, loan.as_of, harness.rules)
    assert result.verdict == "flagged" and result.severity == "low"
    assert result.data_integrity == "defect"
    assert result.primary_reason
    assert "loan_purpose.12" not in result.primary_reason


def test_principal_gap_severity_tiers(harness: Harness) -> None:
    # Diary principal 1200 vs Loan amount: small gap corroborated by the summary -> low;
    # small gap the summary contradicts -> medium; material gap -> high.
    corroborated = SyntheticLoan()
    corroborated.summary_overrides["Loan amount"] = 1205.0
    r = harness.evaluate(corroborated)
    b1 = next(f for f in r.findings if f.rule_id == "B1")
    assert b1.severity == "low" and "only the Loan amount is off" in b1.message
    # The header being off is B2's finding (summary contradicts itself); B1 stays a footnote.
    assert next(f for f in r.findings if f.rule_id == "B2").severity == "critical"

    contradicted = SyntheticLoan()
    contradicted.summary_overrides["Loan amount"] = 1205.0
    contradicted.summary_overrides["Repaid principal"] = 1205.0
    r = harness.evaluate(contradicted)
    b1 = next(f for f in r.findings if f.rule_id == "B1")
    assert b1.severity == "medium" and "not traceable to any diary row" in b1.message
    assert r.verdict == "flagged"

    material = SyntheticLoan()
    material.summary_overrides["Loan amount"] = 2400.0
    material.summary_overrides["Repaid principal"] = 2400.0
    r = harness.evaluate(material)
    b1 = next(f for f in r.findings if f.rule_id == "B1")
    assert b1.severity == "high" and "falls short of Loan amount EUR 2,400.00" in b1.message
    assert b1.evidence["gap"] == -1200.0


def test_as_of_from_config_beats_inference(config: Config, tmp_path: Path) -> None:
    path = tmp_path / "tape.csv"
    make_frame(SyntheticLoan()).to_csv(path, index=False)
    pinned = config.model_copy(update={"as_of": date(2025, 6, 30)})
    report = run(path, pinned)
    assert report.as_of == date(2025, 6, 30)
    assert report.as_of_source == "config"
    inferred = run(path, config)
    assert inferred.as_of_source == "inferred:max_date_in_tape"
    assert inferred.as_of == date(2025, 1, 15)


def test_inferred_as_of_resting_on_one_far_date_is_called_out(
    config: Config, tmp_path: Path
) -> None:
    loan = SyntheticLoan()
    loan.rows[-1].paid = date(2026, 6, 1)  # one future-dated row becomes the max date
    path = tmp_path / "tape.csv"
    make_frame(loan).to_csv(path, index=False)
    report = run(path, config)
    assert report.as_of == date(2026, 6, 1)
    assert "isolated" in report.as_of_source and "pass --as-of" in report.as_of_source
    # The inference hides the row from D2; an explicit as-of exposes it.
    assert not any(f.rule_id == "D2" for f in report.loans[0].findings)
    pinned = run(path, config.model_copy(update={"as_of": date(2025, 2, 1)}))
    assert any(f.rule_id == "D2" for f in pinned.loans[0].findings)


def test_circuit_breaker_trips_when_most_loans_fail(config: Config, tmp_path: Path) -> None:
    loans = []
    for i in range(1, 11):
        loan = SyntheticLoan(loan_id=i)
        if i <= 6:
            loan.summary_overrides["Repaid interest"] = 1.0
        loans.append(loan)
    path = tmp_path / "tape.csv"
    make_frame(*loans).to_csv(path, index=False)
    report = run(path, config)
    assert report.circuit_breaker_tripped
    assert any(f.rule_id == "I3" for f in report.tape_findings)
    # Per-loan findings are still written so an analyst can inspect them.
    assert sum(1 for r in report.loans if r.verdict == "flagged") == 6


def test_circuit_breaker_counts_integrity_severity_not_credit_severity(
    config: Config, tmp_path: Path
) -> None:
    # 4 loans: a critical credit event (F2) each, no data defect. Overall severity is critical,
    # but nothing is wrong with the tape's integrity - the breaker must stay at zero.
    loans = []
    for i in range(1, 5):
        loan = SyntheticLoan(loan_id=i, paid_through=6)
        loan.summary_overrides["Days late"] = 120
        loans.append(loan)
    path = tmp_path / "tape.csv"
    make_frame(*loans).to_csv(path, index=False)
    as_of = loans[0].rows[12].due + timedelta(days=120)
    report = run(path, config.model_copy(update={"as_of": as_of}))
    assert all(r.severity == "critical" and r.data_integrity == "clean" for r in report.loans)
    i3 = next(c for c in report.tape_checks if c.rule_id == "I3")
    assert i3.evidence["severe_defects"] == 0
    assert not report.circuit_breaker_tripped


def test_duplicate_loan_ids_are_a_tape_finding(config: Config, tmp_path: Path) -> None:
    path = tmp_path / "tape.csv"
    make_frame(SyntheticLoan(loan_id=5), SyntheticLoan(loan_id=5)).to_csv(path, index=False)
    report = run(path, config)
    dup = next(f for f in report.tape_findings if f.rule_id == "I1")
    assert "5" in dup.message


def test_same_borrower_contradictory_demographics(config: Config, tmp_path: Path) -> None:
    a = SyntheticLoan(loan_id=1, borrower_id=9)
    b = SyntheticLoan(loan_id=2, borrower_id=9)
    b.summary_overrides["Birth year"] = 1960.0
    path = tmp_path / "tape.csv"
    make_frame(a, b).to_csv(path, index=False)
    report = run(path, config)
    assert any(f.rule_id == "I2" for f in report.tape_findings)


@pytest.mark.parametrize("seed", [0, 1])
def test_run_is_deterministic(config: Config, tmp_path: Path, seed: int) -> None:
    path = tmp_path / f"tape{seed}.csv"
    loan = SyntheticLoan()
    loan.summary_overrides["Repaid interest"] = 1.0
    make_frame(loan, SyntheticLoan(loan_id=2)).to_csv(path, index=False)
    first = run(path, config).to_dict()
    second = run(path, config).to_dict()
    first_meta, second_meta = first["meta"], second["meta"]
    assert isinstance(first_meta, dict) and isinstance(second_meta, dict)
    for volatile in ("generated_at", "duration_seconds"):
        first_meta.pop(volatile)
        second_meta.pop(volatile)
    assert first == second


@pytest.mark.parametrize("field", ["Loan amount", "Interest rate", "Loan term", "Disbursal date"])
def test_blank_required_evidence_is_never_clean(harness: Harness, field: str) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides[field] = ""
    result = harness.evaluate(loan)
    assert result.validation_coverage == "partial"
    assert result.data_integrity in {"unknown", "defect"}
    assert loan_tier(result) != "clean"


@pytest.mark.parametrize("skipping", [False, True])
def test_no_evaluated_rules_is_unknown(harness: Harness, skipping: bool) -> None:
    loan = harness.ingest_one(SyntheticLoan())
    result = evaluate_loan(
        loan, harness.config, date(2025, 2, 1), [SkippingRule()] if skipping else []
    )
    assert result.validation_coverage == "partial"
    assert result.data_integrity == "unknown"


def test_valid_not_applicable_rules_do_not_degrade_coverage(harness: Harness) -> None:
    result = harness.evaluate(SyntheticLoan())
    assert result.checks_not_evaluable > 0
    assert result.validation_coverage == "full"
    assert result.data_integrity == "clean"


@pytest.mark.parametrize("paid_through", [0, 6])
def test_live_loan_not_applicable_checks_keep_full_coverage(
    harness: Harness, paid_through: int
) -> None:
    result = harness.evaluate(SyntheticLoan(paid_through=paid_through))
    assert result.validation_coverage == "full"
    assert result.data_integrity == "clean"


def test_missing_evidence_stays_unknown_when_parse_rule_is_disabled(harness: Harness) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Interest rate"] = ""
    parsed = harness.ingest_one(loan)
    config = harness.config.model_copy(update={"disabled_rules": ["A3"]})
    result = evaluate_loan(parsed, config, loan.as_of, harness.rules)
    assert result.validation_coverage == "partial"
    assert result.data_integrity == "unknown"


def test_rule_error_without_defect_evidence_is_unknown(harness: Harness) -> None:
    loan = harness.ingest_one(SyntheticLoan())
    result = evaluate_loan(loan, harness.config, date(2025, 2, 1), [ExplodingRule()])
    assert result.data_integrity == "unknown"


def test_missing_as_of_requires_explicit_date(tmp_path: Path, config: Config) -> None:
    from loan_dq.engine import AsOfError

    loan = SyntheticLoan()
    loan.summary_overrides.update({"Disbursal date": "", "Repayment date": "", "payments": "[]"})
    path = tmp_path / "undated.csv"
    loan.frame().to_csv(path, index=False)
    with pytest.raises(AsOfError, match="as-of"):
        run(path, config)


@pytest.mark.parametrize("as_of", [None, "2025-02-01"])
def test_cli_rejects_all_quarantined(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], as_of: str | None
) -> None:
    from loan_dq.cli import main

    loan = SyntheticLoan()
    secret = "private.person@example.org"
    loan.summary_overrides["Loan ID"] = secret
    path = tmp_path / "quarantined.csv"
    loan.frame().to_csv(path, index=False)
    args = ["run", str(path), "--out", str(tmp_path / "out"), "--tables", "none", "--quiet"]
    if as_of:
        args.extend(["--as-of", as_of])
    assert main(args) != 0
    assert secret not in capsys.readouterr().err


@pytest.mark.parametrize("count", [0, -1, 2])
def test_invalid_shard_plans_rejected(tmp_path: Path, count: int) -> None:
    from loan_dq.engine import ShardError, plan_shards

    path = tmp_path / "tape.csv"
    SyntheticLoan().frame().to_csv(path, index=False)
    with pytest.raises(ShardError):
        plan_shards(path, count)


def test_quarantine_logs_redact_raw_identifier(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, config: Config
) -> None:
    bad = SyntheticLoan(loan_id=2)
    secret = "private.person@example.org"
    bad.summary_overrides["Loan ID"] = secret
    path = tmp_path / "mixed.csv"
    make_frame(SyntheticLoan(), bad).to_csv(path, index=False)
    run(path, config)
    assert secret not in caplog.text


@pytest.mark.parametrize("field", ["Employment status", "Borrower income"])
def test_optional_context_does_not_invalidate_financial_coverage(
    harness: Harness, field: str
) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides[field] = ""
    result = harness.evaluate(loan)
    assert next(o for o in result.rule_outcomes if o.rule_id == "H10").status == "not_evaluable"
    assert result.validation_coverage == "full"
    assert result.data_integrity == "clean"
