"""Calibration against the supplied tape (data/loans.xlsx).

These pin the behaviour the assignment brief describes: the two loans it names as clean stay
normal, the loan it names as anomalous is critical, and the engine neither over- nor under-flags
the tape as a whole. Skipped when the workbook is not present (e.g. a CI checkout without data).
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from loan_dq.config import Config
from loan_dq.engine import evaluate_loan, run
from loan_dq.ingest.model import Loan, PaymentType
from loan_dq.ingest.schema import ingest
from loan_dq.io.reader import read_tape
from loan_dq.report.schema import LoanResult, Report
from loan_dq.rules.base import LoanContext, paid_portion, pending_portion
from loan_dq.rules.registry import all_rules
from tests.conftest import WORKBOOK

pytestmark = pytest.mark.skipif(not WORKBOOK.exists(), reason="data/loans.xlsx not present")

CLEAN_LOANS = (32271989, 99981632)
SEEDED_DEFAULT = 37216892


@pytest.fixture(scope="module")
def report(config: Config) -> Report:
    return run(WORKBOOK, config)


@pytest.fixture(scope="module")
def parsed(config: Config) -> dict[int, Loan]:
    """The ingested tape, for deriving expected numbers from the rows independently of the rules."""
    return {loan.loan_id: loan for loan in ingest(read_tape(WORKBOOK), config).loans}


def by_id(report: Report, loan_id: int) -> LoanResult:
    return next(r for r in report.loans if r.loan_id == loan_id)


def test_every_row_is_accounted_for(report: Report) -> None:
    assert report.rows_read == 72
    assert len(report.loans) + len(report.quarantined) == 72
    assert report.schema_ok
    assert not report.circuit_breaker_tripped
    assert len({r.loan_id for r in report.loans}) == len(report.loans)


@pytest.mark.parametrize("loan_id", CLEAN_LOANS)
def test_known_clean_loans_are_normal(report: Report, loan_id: int) -> None:
    result = by_id(report, loan_id)
    assert result.verdict == "normal", [f.message for f in result.findings]
    assert result.data_integrity == "clean"
    assert result.credit_event == "none"


def test_clean_loan_with_rounded_loan_amount_passes_principal_identity(report: Report) -> None:
    result = by_id(report, 32271989)
    assert result.residuals["B2_gap"] != 0.0
    assert abs(float(str(result.residuals["B2_gap"]))) < 0.5


def test_habitually_late_clean_loan_is_reported_as_behaviour_not_defect(report: Report) -> None:
    result = by_id(report, 99981632)
    profile = result.behaviour_profile
    assert profile is not None
    late_count, worst = profile["paid_late_count"], profile["worst_delay_days"]
    assert isinstance(late_count, int) and isinstance(worst, int)
    assert late_count > 0
    assert worst < 30
    assert all(f.severity == "info" for f in result.findings)


def test_seeded_default_is_critical_with_reason(report: Report) -> None:
    result = by_id(report, SEEDED_DEFAULT)
    assert result.verdict == "flagged"
    assert result.severity == "critical"
    assert result.credit_event == "default"
    f2 = next(f for f in result.findings if f.rule_id == "F2")
    assert f2.root_cause
    assert "90+ days past due" in f2.message
    assert "days overdue" in f2.message


def test_historical_90_day_defaults_are_found(report: Report) -> None:
    for loan_id in (31397492, 61752881, 79811839, 96579687):
        result = by_id(report, loan_id)
        assert result.credit_event == "default", loan_id
        assert "F1" in {f.rule_id for f in result.findings}, loan_id


def test_repaid_status_with_open_obligations_is_flagged(report: Report) -> None:
    result = by_id(report, 94863476)
    assert result.data_integrity == "defect"
    assert "E1" in {f.rule_id for f in result.findings if f.root_cause}


def test_deferred_annuities_settled_early_are_low_confidence_reviews(
    report: Report, parsed: dict[int, Loan]
) -> None:
    # Five deferred annuities were settled before any scheduled interest instalment fell due and
    # realised (almost) no interest. Whether early settlement waives the deferred interest is a
    # contract term the tape does not carry, so the XIRR mismatch stays a finding - worded as a
    # review item and carrying confidence: low derived from the loan's own dates, not its ID.
    early = [
        loan_id
        for loan_id, loan in parsed.items()
        if loan.is_deferred_annuity
        and loan.repayment_date is not None
        and loan.expected_repayment_date is not None
        and loan.repayment_date < loan.expected_repayment_date
        and not any(
            p.type is PaymentType.INTEREST
            and not p.is_closure_row
            and p.due_date is not None
            and p.due_date < loan.repayment_date
            for p in loan.diary.payments
        )
    ]
    assert sorted(early) == [14146974, 17611322, 35294697, 46313736, 58271697]
    for loan_id in early:
        result = by_id(report, loan_id)
        c2 = next(f for f in result.findings if f.rule_id == "C2")
        assert c2.evidence["confidence"] == "low", loan_id
        assert c2.severity == "low", loan_id
        assert "not a confirmed rate defect" in c2.message, loan_id
        assert "review against early-settlement/deferred-annuity terms" in c2.message, loan_id
        assert result.credit_event == "none", loan_id
        if loan_id == 14146974:
            assert result.verdict == "normal" and result.data_integrity == "clean"
            assert c2.root_cause
            assert result.residuals["B1_gap"] == result.residuals["B3_gap"] == 0.0
            assert parsed[loan_id].loan_amount == parsed[loan_id].repaid_principal == 135.0
            assert not any(f.rule_id == "B1" for f in result.findings)
        else:
            assert result.verdict == "flagged"
            assert c2.symptom_of == "B1"
            b1 = next(f for f in result.findings if f.rule_id == "B1")
            assert b1.root_cause and b1.evidence["gap"] < -0.5
    # Deferred annuities that ran to their interest schedule are measured at full confidence.
    for result in report.loans:
        c2 = next((f for f in result.findings if f.rule_id == "C2"), None)
        if c2 is not None and result.loan_id not in early:
            if c2.evidence["linked_principal_rule"] == "B1":
                assert c2.evidence["confidence"] == "low" and c2.symptom_of == "B1"
            else:
                assert c2.evidence["confidence"] == "high", result.loan_id


def test_principal_shortfall_is_not_misreported_as_zero_interest(
    report: Report, parsed: dict[int, Loan]
) -> None:
    result = by_id(report, 76669736)
    loan = parsed[76669736]
    ordinary = sum(paid_portion(p) for p in loan.diary.payments if p.type is PaymentType.INTEREST)
    overdue = sum(
        paid_portion(p) for p in loan.diary.payments if p.type is PaymentType.OVERDUE_INTEREST
    )
    assert ordinary == pytest.approx(12.58) and overdue == pytest.approx(0.06)
    c2 = next(f for f in result.findings if f.rule_id == "C2")
    b1 = next(f for f in result.findings if f.rule_id == "B1")
    assert result.verdict == "flagged" and b1.root_cause
    assert b1.evidence["gap"] == -22.76
    assert c2.symptom_of == "B1" and c2.severity == "low"
    assert c2.evidence["paid_interest_total"] == 12.64
    assert "EUR 12.64 interest actually paid" in c2.message
    assert "not a confirmed rate defect" in c2.message


def test_on_time_label_paid_beyond_grace_is_a_low_observation(report: Report) -> None:
    result = by_id(report, 53788773)
    d8 = next(f for f in result.findings if f.rule_id == "D8")
    assert d8.severity == "low" and d8.evidence["rows"] == 2
    assert "2024-06-19" in d8.message and "6 days" in d8.message
    assert result.verdict == "normal" and result.credit_event == "none"


def test_overdue_amount_is_what_is_still_owed(
    report: Report, parsed: dict[int, Loan], config: Config
) -> None:
    # Derive F2's numbers from the rows: unpaid regular components due 90+ days before as-of.
    loan = parsed[37216892]
    as_of = report.as_of
    threshold = config.lateness.default_days
    selected = [
        p
        for p in loan.diary.payments
        if p.is_regular
        and p.type in (PaymentType.PRINCIPAL, PaymentType.INTEREST, PaymentType.FEE)
        and not p.state.is_paid
        and p.due_date is not None
        and (as_of - p.due_date).days >= threshold
    ]
    amount = round(sum(pending_portion(p) for p in selected), 2)
    gross = round(sum(p.amount or 0.0 for p in selected), 2)
    dues = {p.due_date for p in selected}
    assert (len(selected), len(dues), amount, gross) == (5, 2, 755.66, 787.01)

    f2 = next(f for f in by_id(report, 37216892).findings if f.rule_id == "F2")
    assert f2.evidence["components_overdue"] == len(selected)
    assert f2.evidence["due_dates_overdue"] == len(dues)
    assert f2.evidence["overdue_amount"] == amount
    assert f2.message.startswith(
        f"{len(selected)} payment component(s) across {len(dues)} due date(s) with EUR "
        f"{amount:,.2f} still unpaid"
    )


def test_terminated_unresolved_amount_is_derived_from_pending_rows(
    report: Report, parsed: dict[int, Loan]
) -> None:
    loan = parsed[37216892]
    assert loan.is_terminated
    unpaid = [p for p in loan.diary.payments if p.is_regular and not p.state.is_paid]
    claims = [
        p for p in loan.diary.payments if p.type is PaymentType.TERMINATION and not p.state.is_paid
    ]
    unpaid_amount = round(sum(pending_portion(p) for p in unpaid), 2)
    claim_amount = round(sum(pending_portion(p) for p in claims), 2)
    unresolved = round(unpaid_amount + claim_amount, 2)
    assert unresolved == 4043.23

    e3 = next(f for f in by_id(report, 37216892).findings if f.rule_id == "E3")
    assert e3.evidence["unresolved_amount"] == unresolved
    assert e3.evidence["unpaid_components"] == len(unpaid)
    assert e3.evidence["unpaid_due_dates"] == len({p.due_date for p in unpaid})
    assert e3.evidence["unpaid_amount"] == unpaid_amount
    assert e3.evidence["pending_termination_rows"] == len(claims)
    assert e3.evidence["pending_termination_amount"] == claim_amount
    assert f"EUR {unresolved:,.2f} unresolved" in e3.message
    assert f"{len(unpaid)} unpaid payment component(s)" in e3.message


def test_conflicting_fee_rows_in_one_slot_are_a_review_observation(report: Report) -> None:
    # 41531189: two contract-fee rows for the same due date, EUR 2.07 and EUR 2.65, paid a day
    # apart. The only same-slot conflict on the tape; a review candidate, not a proven defect.
    hits = [r for r in report.loans if any(f.rule_id == "C10" for f in r.findings)]
    assert [r.loan_id for r in hits] == [41531189]
    c10 = next(f for f in hits[0].findings if f.rule_id == "C10")
    assert c10.severity == "low" and hits[0].verdict == "normal"
    slots = c10.evidence["slots"]
    assert isinstance(slots, list) and len(slots) == 1
    assert slots[0]["type"] == "contract fee repayment"
    assert slots[0]["due_date"] == "2025-01-15"
    assert slots[0]["amounts"] == [2.07, 2.65]
    assert slots[0]["differs_in"] == ["amount", "actual_date"]
    for loan_id in CLEAN_LOANS:
        outcome = next(o for o in by_id(report, loan_id).rule_outcomes if o.rule_id == "C10")
        assert outcome.status == "pass", loan_id


def test_circuit_breaker_counts_integrity_severity_only(report: Report) -> None:
    i3 = next(c for c in report.tape_checks if c.rule_id == "I3")
    severe = {
        r.loan_id
        for r in report.loans
        if r.data_integrity == "defect"
        and any(
            f.root_cause and f.axis == "integrity" and f.severity in {"high", "critical"}
            for f in r.findings
        )
    }
    assert i3.evidence["severe_defects"] == len(severe)
    assert severe == {94863476, 65318525, 79811839}
    # 31397492 is high through its credit event, not a major integrity finding.
    assert 31397492 not in severe and by_id(report, 31397492).severity == "high"


def test_flags_follow_evidence_roots_not_a_population_rate(report: Report, config: Config) -> None:
    # Unknown seed labels cannot establish an expected flag rate or measured recall.
    # Each verdict must follow its evidence roots rather than a population target.
    ranks = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    for result in report.loans:
        roots = [f for f in result.findings if f.root_cause]
        expected = any(ranks[f.severity] >= ranks[config.verdict.flag_min_severity] for f in roots)
        assert (result.verdict == "flagged") is expected, result.loan_id
        for symptom in (f for f in result.findings if not f.root_cause):
            assert any(f.rule_id == symptom.symptom_of for f in result.findings), result.loan_id


def test_truncated_diaries_are_partial_coverage_not_defects(report: Report) -> None:
    truncated = [r for r in report.loans if any(f.rule_id == "A8" for f in r.findings)]
    assert len(truncated) == 13
    for r in truncated:
        assert r.validation_coverage == "partial"
        assert r.data_integrity != "clean"
        assert all(f.severity == "info" for f in r.findings if f.rule_id == "A8")


def test_every_flagged_loan_has_a_plain_english_reason(report: Report) -> None:
    for r in report.loans:
        if r.verdict == "flagged":
            assert r.primary_reason and r.primary_reason != "no anomalies detected", r.loan_id
            assert all(f.message for f in r.findings), r.loan_id
        elif any(f.severity != "info" for f in r.findings):
            # Low findings stay visible on a normal loan instead of being hidden.
            assert r.primary_reason.startswith("no major issues; minor observation:"), r.loan_id
            assert r.severity == "low", r.loan_id
        elif r.findings:  # info only (e.g. truncated diary): the note itself is the reason
            assert r.primary_reason == r.findings[0].message, r.loan_id
        elif r.validation_coverage != "full":
            assert "validation incomplete" in r.primary_reason, r.loan_id
        else:
            assert r.primary_reason == "no anomalies detected", r.loan_id


def test_low_only_findings_do_not_flag_the_loan(report: Report) -> None:
    # Untranslated purpose codes and 30-59 day paid delays are reported, not flagged.
    for loan_id in (94273288, 56689556, 57949284):
        r = by_id(report, loan_id)
        assert r.verdict == "normal", (loan_id, r.primary_reason)
        assert r.severity == "low"
        assert r.findings and any(f.severity == "low" for f in r.findings)


def test_watch_band_lateness_keeps_credit_axis(report: Report) -> None:
    # F3 informs the credit axis whether or not it reaches the flag floor.
    watch = {r.loan_id for r in report.loans if r.credit_event == "watch"}
    assert {56689556, 57949284, 53926762, 73224837} <= watch
    assert by_id(report, 53926762).verdict == "flagged"  # 89-day paid delay
    assert by_id(report, 56689556).verdict == "normal"  # 35-day paid delays


def test_principal_shortfall_wording_is_evidence_based(report: Report) -> None:
    b1 = next(f for f in by_id(report, 65318525).findings if f.rule_id == "B1")
    assert b1.severity == "high"
    assert "falls short of Loan amount" in b1.message
    assert "confirm lender" not in b1.message
    assert b1.evidence["summary_principal_total"] == 5850.0
    small = next(f for f in by_id(report, 17611322).findings if f.rule_id == "B1")
    assert small.severity == "medium"
    assert "not traceable to any diary row" in small.message


def test_tape_checks_are_all_reported(report: Report) -> None:
    by_rule = {c.rule_id: c for c in report.tape_checks}
    assert set(by_rule) == {"I1", "I2", "I3", "I4", "I5", "I6"}
    assert all(c.status == "pass" for r, c in by_rule.items() if r != "I4"), [
        (c.rule_id, c.status) for c in by_rule.values()
    ]
    # 72 loans seed too few independent amounts for a Benford verdict: statistics, no verdict.
    i4 = by_rule["I4"]
    assert i4.status == "not_evaluable" and "reported for information" in i4.message
    assert i4.evidence["sample"] >= 500 and i4.evidence["loans"] < i4.evidence["min_loans"]
    assert set(i4.evidence["segments"]) >= {"status=granted", "status=repaid"}
    assert by_rule["I5"].evidence["shared_rows"] == 0
    assert by_rule["I6"].evidence["round_share"] < 0.02
    assert report.tape_findings == []
    assert report.flagged_profile["flagged"] == sum(
        1 for r in report.loans if r.verdict == "flagged"
    )


def test_four_tier_summary_adds_up(report: Report) -> None:
    tiers = report.counts()["tiers"]
    assert isinstance(tiers, dict)
    assert tiers["major"] == sum(1 for r in report.loans if r.verdict == "flagged")
    assert tiers["minor"] == sum(
        1
        for r in report.loans
        if r.verdict == "normal"
        and r.validation_coverage == "full"
        and any(f.severity != "info" for f in r.findings)
    )
    assert tiers["major"] + tiers["minor"] + tiers["indeterminate"] + tiers["clean"] == len(
        report.loans
    )
    # No finding on a truncated diary is absence of evidence, not a clean loan.
    assert tiers["indeterminate"] == sum(
        1 for r in report.loans if r.validation_coverage != "full" and r.verdict != "flagged"
    )
    assert tiers["indeterminate"] >= 13
    assert tiers["major"] == 15
    assert tiers["clean"] == sum(
        1
        for r in report.loans
        if r.validation_coverage == "full" and not any(f.severity != "info" for f in r.findings)
    )
    for r in report.loans:
        if r.loan_id in {32271989, 99981632}:
            assert not any(f.severity != "info" for f in r.findings)
            assert r.validation_coverage == "full"


def test_tape_health_measures_the_export_not_the_loans(report: Report) -> None:
    health = report.tape_health
    assert health is not None
    assert health.schema.export_timestamp_supplied is False
    assert ["collaretal", "collateral"] in health.schema.header_words_with_transposed_letters
    assert "Days late" in health.columns.low_information_columns
    assert health.columns.low_information_columns["Days late"].dominant_value == "0"
    integer_valued = set(health.columns.decimal_columns_stored_as_integers)
    assert {"Loan amount", "Interest rate"} <= integer_valued
    assert len(health.columns.columns_100pct_empty) == 4
    assert health.dates.single_format is False
    assert health.dates.distinct_formats_in_file == 2
    assert health.payments_diary.diaries_truncated == 13
    assert health.payments_diary.cells_at_excel_limit == 13
    assert health.payments_diary.diaries_unreadable == 0
    assert health.payments_diary.loans_with_record_of_another_loan == 0
    assert health.vocabulary.untranslated_codes == {"Purpose": {"rows": 5}}
    assert health.vocabulary.categorical_values_outside_dictionary == {}
    lines = health.lines()
    assert any("Excel cell limit" in line for line in lines)
    assert any("2 formats in one file" in line for line in lines)


def test_clean_workbook_isolated_material_interest_mutation(
    parsed: dict[int, Loan], report: Report, config: Config
) -> None:
    loan = deepcopy(parsed[32271989])
    row = next(p for p in loan.diary.payments if p.type is PaymentType.INTEREST)
    assert row.amount == row.pending_amount == 2.90
    row.amount = row.pending_amount = 102.90
    assert loan.outstanding_interest is not None
    loan.outstanding_interest += 100.0
    result = evaluate_loan(loan, config, report.as_of, all_rules())
    assert result.verdict == "flagged"
    c1 = next(f for f in result.findings if f.rule_id == "C1")
    assert c1.root_cause and "isolated" in c1.message
    assert c1.evidence["examples"][0]["interest_row"] == 102.90
    assert not any(f.rule_id == "B5" for f in result.findings)


def test_paid_label_overdue_interest_reconciles_using_net_receipts(
    parsed: dict[int, Loan], report: Report, config: Config
) -> None:
    loan = parsed[79811839]
    rows = [p for p in loan.diary.payments if p.type is PaymentType.OVERDUE_INTEREST]
    assert round(sum(p.amount or 0.0 for p in rows), 2) == 92.31
    assert round(sum(p.pending_amount or 0.0 for p in rows), 2) == 55.75
    assert round(sum(paid_portion(p) for p in rows), 2) == loan.delay_interest == 36.56
    assert round(sum(pending_portion(p) for p in rows), 2) == 55.75
    result = by_id(report, loan.loan_id)
    assert not any(f.rule_id == "B6" for f in result.findings)
    assert {"C6", "E1", "F1"} <= {f.rule_id for f in result.findings}
    ctx = LoanContext(loan, config, report.as_of)
    assert sum(paid_portion(p) for p in ctx.settlement_cash) == pytest.approx(584.72)
    assert all(paid_portion(p) == 0.0 for p in loan.diary.payments if p.is_settlement_marker)


def test_three_group_evaluation_preserves_legacy_decisions_and_evidence(config: Config) -> None:
    from loan_dq.rules import (
        arithmetic,
        behaviour,
        identities,
        lateness,
        lifecycle,
        plausibility,
        readability,
        temporal,
    )

    legacy_order = [
        *readability.RULES,
        *identities.RULES,
        *arithmetic.RULES,
        *temporal.RULES,
        *lifecycle.RULES,
        *lateness.RULES,
        *behaviour.RULES,
        *plausibility.RULES,
    ]
    legacy = run(WORKBOOK, config, rules=legacy_order)
    grouped = run(WORKBOOK, config)
    assert grouped.counts() == legacy.counts()
    assert grouped.tape_checks == legacy.tape_checks
    assert grouped.tape_findings == legacy.tape_findings
    assert grouped.publication_status == legacy.publication_status
    for before, after in zip(legacy.loans, grouped.loans, strict=True):
        before_dict, after_dict = before.to_dict(), after.to_dict()
        for field in ("findings", "rule_outcomes", "rule_ids"):
            key = (lambda r: r["rule_id"]) if field != "rule_ids" else None
            before_dict[field] = sorted(before_dict[field], key=key)
            after_dict[field] = sorted(after_dict[field], key=key)
        assert after_dict == before_dict, before.loan_id
