"""Tape-level digit forensics (I4 Benford on distinct amounts, I5 shared diary rows, I6 round
cents). Each screen is checked three ways: it stays quiet on a naturally generated population,
it fires on a seeded defect and names it, and it refuses a verdict when the sample is too small.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import replace

from loan_dq.config import Config
from loan_dq.ingest.schema import ingest
from loan_dq.rules.tape import (
    LoanFacts,
    benford_first_digit,
    borrower_demographics_consistent,
    facts_of,
    round_number_screen,
    shared_diary_rows,
)
from tests.conftest import DiaryRow, Harness, SyntheticLoan, make_frame


def test_borrower_comparison_facts_are_opaque_and_lender_scoped(harness: Harness) -> None:
    first = harness.ingest_one(SyntheticLoan(loan_id=1, borrower_id=777))
    second = harness.ingest_one(SyntheticLoan(loan_id=2, borrower_id=777))
    second.birth_year = 1973
    a = LoanFacts.from_loan(first, lender_scope="lender-a")
    b = LoanFacts.from_loan(second, lender_scope="lender-a")
    foreign = LoanFacts.from_loan(second, lender_scope="lender-b")
    doc = a.to_dict()
    assert not {"borrower_id", "birth_year", "borrower_type"} & doc.keys()
    assert len(doc["borrower_token"]) == 64
    assert len(doc["demographics_digest"]) == 64
    assert doc["borrower_token"] != foreign.to_dict()["borrower_token"]
    assert borrower_demographics_consistent([a, b]).status == "fail"
    assert borrower_demographics_consistent([a, foreign]).status == "pass"
    assert LoanFacts.from_dict(doc).to_dict() == doc


def _natural_cents(rng: random.Random, n: int) -> list[int]:
    """Log-uniform amounts over 4 orders of magnitude: Benford-conformant with random cents."""
    return sorted({round(10 ** rng.uniform(2.0, 6.0)) for _ in range(n)})


def _population(rng: random.Random, loans: int, amounts_per_loan: int) -> list[LoanFacts]:
    return [
        LoanFacts(
            loan_id=1000 + i,
            borrower_token=hashlib.sha256(str(i).encode()).hexdigest(),
            demographics_digest=hashlib.sha256(str(i).encode()).hexdigest(),
            source_row_index=i,
            loan_status="granted" if i % 2 else "repaid",
            loan_type="instalment",
            vintage=2022 + i % 3,
            days_late=0,
            paid_cents=_natural_cents(rng, amounts_per_loan),
            paid_rows=[
                hashlib.sha256(f"{i}|{k}|principal|{c}".encode()).hexdigest()
                for k, c in enumerate(_natural_cents(rng, amounts_per_loan))
            ],
            free_cents=_natural_cents(rng, amounts_per_loan),
        )
        for i in range(loans)
    ]


# --- I4 --------------------------------------------------------------------------------------


def test_benford_passes_on_a_natural_population_and_reports_all_three_statistics(
    config: Config,
) -> None:
    facts = _population(random.Random(1), loans=300, amounts_per_loan=20)
    check = benford_first_digit(facts, config)
    assert check.status == "pass", check.message
    ev = check.evidence
    assert ev["loans"] == 300 and ev["sample"] >= config.tape.benford_min_sample
    assert ev["first_digit_mad"] <= config.tape.benford_max_mad
    assert "first_two_digits_mad" in ev and "summation_share" in ev
    assert abs(sum(ev["summation_share"].values()) - 1) < 0.01
    assert {"status=granted", "status=repaid", "loan_type=instalment"} <= set(ev["segments"])


def test_benford_fails_on_a_generated_population_and_localises_the_segment(config: Config) -> None:
    facts = _population(random.Random(2), loans=300, amounts_per_loan=20)
    # A 2024 batch whose amounts were drawn uniformly (a lazy generator): first digits flat.
    rng = random.Random(3)
    for f in facts:
        if f.vintage == 2024:
            f.paid_cents = sorted({rng.randrange(10_000, 99_999) for _ in range(40)})
    check = benford_first_digit(facts, config)
    assert check.status == "fail"
    assert "deviates from Benford" in check.message
    seg = check.evidence["segments"]
    assert seg["vintage=2024"]["first_digit_mad"] > seg["vintage=2022"]["first_digit_mad"]
    assert seg["vintage=2024"]["first_digit_band"] == "nonconformity"
    assert check.evidence["heaviest_digit_by_value"]["top_loan"] in {f.loan_id for f in facts}


def test_benford_counts_a_repeated_instalment_once(config: Config) -> None:
    facts = _population(random.Random(4), loans=300, amounts_per_loan=20)
    inflated = [replace(f, paid_rows=f.paid_rows * 50) for f in facts]
    assert (
        benford_first_digit(inflated, config).evidence["sample"]
        == (benford_first_digit(facts, config).evidence["sample"])
    )


def test_benford_gives_no_verdict_below_the_loan_floor(config: Config) -> None:
    few = _population(random.Random(5), loans=60, amounts_per_loan=30)
    check = benford_first_digit(few, config)
    assert check.status == "not_evaluable"
    assert "reported for information" in check.message
    assert check.evidence["sample"] >= config.tape.benford_min_sample
    assert check.evidence["first_digit_mad"] is not None

    tiny = _population(random.Random(6), loans=5, amounts_per_loan=5)
    check = benford_first_digit(tiny, config)
    assert check.status == "not_evaluable" and "need 500" in check.message


def test_benford_declines_when_amounts_span_under_two_orders_of_magnitude(config: Config) -> None:
    rng = random.Random(7)
    facts = _population(rng, loans=300, amounts_per_loan=10)
    for f in facts:
        f.paid_cents = sorted({rng.randrange(10_000, 30_000) for _ in range(10)})
    check = benford_first_digit(facts, config)
    assert check.status == "not_evaluable" and "orders of magnitude" in check.message


# --- I5 --------------------------------------------------------------------------------------


def test_shared_rows_pass_on_distinct_loans_and_list_shared_amounts_as_context(
    config: Config,
) -> None:
    facts = _population(random.Random(8), loans=50, amounts_per_loan=20)
    facts[0].paid_cents.append(12_345)
    facts[1].paid_cents.append(12_345)
    check = shared_diary_rows(facts, config)
    assert check.status == "pass" and check.evidence["shared_rows"] == 0
    assert {"amount": 123.45, "loans": [1000, 1001]} in check.evidence["shared_amount_examples"]


def test_shared_rows_fail_when_one_loans_diary_rows_appear_under_another(config: Config) -> None:
    facts = _population(random.Random(9), loans=50, amounts_per_loan=20)
    facts[7].paid_rows = facts[7].paid_rows + facts[3].paid_rows[:4]
    check = shared_diary_rows(facts, config)
    assert check.status == "fail" and check.severity == "high"
    assert check.evidence["shared_rows"] == 4
    assert check.evidence["loans"] == [1003, 1007]
    assert all(ex["loans"] == [1003, 1007] for ex in check.evidence["examples"])


def test_shared_rows_through_ingestion_name_both_loans(config: Config, harness: Harness) -> None:
    a = SyntheticLoan(loan_id=1)
    b = SyntheticLoan(loan_id=2, amount=3400.0, annual_rate=9.5, term=18, paid_through=6)
    b.rows = b.rows + a.rows[:2]  # two of loan 1's paid rows pasted into loan 2's diary
    result = ingest(make_frame(a, b), config)
    check = shared_diary_rows(facts_of(result.loans), config)
    assert check.status == "fail" and check.evidence["loans"] == [1, 2]
    assert check.evidence["shared_rows"] == 2

    clean = ingest(make_frame(SyntheticLoan(loan_id=1), replace(b, rows=[])), config)
    assert shared_diary_rows(facts_of(clean.loans), config).status == "pass"

    # Two loans with identical terms, dates and schedule *are* copies of each other.
    twins = ingest(make_frame(SyntheticLoan(loan_id=1), SyntheticLoan(loan_id=2)), config)
    assert shared_diary_rows(facts_of(twins.loans), config).status == "fail"


# --- I6 --------------------------------------------------------------------------------------


def test_round_cents_pass_on_random_cents(config: Config) -> None:
    facts = _population(random.Random(10), loans=50, amounts_per_loan=20)
    check = round_number_screen(facts, config)
    assert check.status == "pass" and check.evidence["round_share"] < 0.03


def test_round_cents_fail_when_computed_amounts_were_rounded(config: Config) -> None:
    facts = _population(random.Random(11), loans=50, amounts_per_loan=20)
    for f in facts[:20]:
        f.free_cents = [c - c % 100 for c in f.free_cents]
        f.days_late = 30 * (1 + f.loan_id % 4)
    for f in facts[20:]:
        f.days_late = 7 + f.loan_id % 50
    check = round_number_screen(facts, config)
    assert check.status == "fail" and check.evidence["round_share"] >= 0.3
    assert "typed by hand" in check.message
    assert 0.3 < check.evidence["days_late_multiple_of_30_share"] < 0.5
    assert "multiples of 30" in check.message


def test_round_cents_not_evaluable_on_a_small_sample(config: Config) -> None:
    facts = _population(random.Random(12), loans=3, amounts_per_loan=5)
    assert round_number_screen(facts, config).status == "not_evaluable"


# --- facts -----------------------------------------------------------------------------------


def test_facts_extract_distinct_paid_amounts_rows_and_free_amounts(harness: Harness) -> None:
    loan = SyntheticLoan(paid_through=6)
    loan.rows.append(DiaryRow(loan.rows[-1].due, None, "interest", "pending", 0.0, 0.0))
    facts = LoanFacts.from_loan(harness.ingest_one(loan))
    assert facts.loan_status == "granted" and facts.loan_type == "instalment"
    assert facts.vintage == 2024 and facts.days_late == 0
    assert len(facts.paid_rows) == 12  # 6 principal + 6 interest paid rows
    assert facts.paid_cents == sorted(set(facts.paid_cents))
    assert all(c > 0 for c in facts.free_cents)
    # 6 paid interest rows + 12 pending amounts + monthly payment, outstanding principal,
    # repaid interest, outstanding interest (arrears None and delay interest 0 are skipped)
    assert len(facts.free_cents) == 6 + 12 + 4


def test_facts_round_trip_through_json_shape() -> None:
    facts = _population(random.Random(13), loans=2, amounts_per_loan=3)[0]
    facts.days_late = None
    assert LoanFacts.from_dict(facts.to_dict()) == facts
