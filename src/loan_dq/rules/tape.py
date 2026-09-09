"""Category I - checks that only make sense across the whole tape.

Every check returns a ``TapeCheck`` (pass / fail / not_evaluable, with the number it looked at),
so the report shows what was screened even when nothing fired. Failures are additionally
surfaced as ``TapeFinding`` entries.

The checks read only ``LoanFacts`` - the handful of per-loan values they need - so a sharded
run can carry those facts out of each shard and ``merge`` evaluates the checks once over the
whole tape, exactly as a single-process run would.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from loan_dq.config import Config
from loan_dq.ingest.model import INTEREST_TYPES, SETTLEMENT_TYPES, Loan
from loan_dq.report.schema import LoanResult, TapeCheck, TapeFinding, public_category
from loan_dq.rules.base import Severity, Status

BENFORD = {d: math.log10(1 + 1 / d) for d in range(1, 10)}
BENFORD_TWO = {d: math.log10(1 + 1 / d) for d in range(10, 100)}
# Nigrini's conformity bands for the mean absolute deviation (first digit / first-two digits).
MAD_BANDS = ((0.006, 0.0006, "close"), (0.012, 0.0012, "acceptable"), (0.015, 0.0022, "marginal"))


def _cents(value: float) -> int:
    return round(value * 100)


def _first_digit(cents: int) -> int:
    return int(str(cents // 100)[0])


@dataclass
class LoanFacts:
    """What the tape-level checks need from one loan.

    Amounts travel as integer cents so a shard report round-trips through JSON exactly.
    ``paid_cents`` is the *distinct* set of paid amounts (an annuity repeats its instalment
    100 times; that is one number, not 100 Benford samples). ``paid_rows`` are the paid diary
    rows as ``due|paid|type|cents`` keys for the cross-loan copy screen. ``free_cents`` are
    the amounts nobody chose - interest, settlements, pending balances, summary balances -
    whose cents should look random.
    """

    loan_id: int
    borrower_token: str | None
    demographics_digest: str | None
    loan_status: str | None = None
    loan_type: str | None = None
    vintage: int | None = None
    days_late: int | None = None
    paid_cents: list[int] = field(default_factory=list)
    paid_rows: list[str] = field(default_factory=list)
    free_cents: list[int] = field(default_factory=list)
    source_row_index: int = 0

    @classmethod
    def from_loan(cls, loan: Loan, lender_scope: str = "default") -> LoanFacts:
        if loan.loan_id is None:
            raise ValueError("LoanFacts needs a parsed Loan ID")
        paid = [p for p in loan.diary.payments if p.state.is_paid and p.amount and p.amount > 0]
        free: list[int] = [
            _cents(p.amount)
            for p in paid
            if p.amount is not None and p.type in INTEREST_TYPES | SETTLEMENT_TYPES
        ]
        free += [
            _cents(p.pending_amount)
            for p in loan.diary.payments
            if p.pending_amount is not None and p.pending_amount > 0
        ]
        free += [
            _cents(v)
            for v in (
                loan.monthly_payment,
                loan.outstanding_principal,
                loan.repaid_interest,
                loan.outstanding_interest,
                loan.arrears,
                loan.delay_interest,
            )
            if v is not None and v > 0
        ]
        return cls(
            loan.loan_id,
            (
                _comparison_digest("borrower", lender_scope, loan.borrower_id)
                if loan.borrower_id is not None
                else None
            ),
            (
                _comparison_digest(
                    "demographics",
                    lender_scope,
                    loan.borrower_id,
                    loan.birth_year,
                    loan.borrower_type,
                )
                if loan.borrower_id is not None
                else None
            ),
            public_category("Loan status", loan.loan_status),
            public_category("Loan type", loan.loan_type),
            loan.disbursal_date.year if loan.disbursal_date else None,
            loan.days_late,
            sorted({_cents(p.amount) for p in paid if p.amount is not None}),
            sorted(
                _comparison_digest(
                    "paid-row", str(p.due_date), str(p.actual_date), p.type.value, _cents(p.amount)
                )
                for p in paid
                if p.amount is not None
            ),
            free,
            loan.row_index,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> LoanFacts:
        if set(d) != set(cls.__dataclass_fields__):
            raise ValueError("unsupported loan facts schema")

        def integer(value: object) -> int:
            if type(value) is not int:
                raise ValueError("loan fact must be an integer")
            return value

        def opt_int(key: str) -> int | None:
            return None if d[key] is None else integer(d[key])

        def digest(value: object) -> str | None:
            if value is None:
                return None
            if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
                raise ValueError("invalid comparison digest")
            return value

        def category(key: str, name: str) -> str | None:
            value = d[key]
            approved = public_category(name, value)
            if value != approved:
                raise ValueError("invalid loan fact category")
            return approved

        def ints(key: str) -> list[int]:
            raw = d[key]
            if not isinstance(raw, list):
                raise ValueError("loan fact must be a list")
            return [integer(v) for v in raw]

        rows = d["paid_rows"]
        if not isinstance(rows, list) or any(digest(r) is None for r in rows):
            raise ValueError("invalid paid row digests")
        result = cls(
            integer(d["loan_id"]),
            digest(d["borrower_token"]),
            digest(d["demographics_digest"]),
            category("loan_status", "Loan status"),
            category("loan_type", "Loan type"),
            opt_int("vintage"),
            opt_int("days_late"),
            ints("paid_cents"),
            list(rows),
            ints("free_cents"),
            integer(d["source_row_index"]),
        )
        if (result.borrower_token is None) != (result.demographics_digest is None):
            raise ValueError("incomplete borrower comparison facts")
        if result.source_row_index < 0 or any(c < 0 for c in result.paid_cents + result.free_cents):
            raise ValueError("negative fact counter")
        return result


def _comparison_digest(*values: object) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()


def facts_of(loans: list[Loan], lender_scope: str = "default") -> list[LoanFacts]:
    return [
        LoanFacts.from_loan(loan, lender_scope=lender_scope)
        for loan in loans
        if loan.loan_id is not None
    ]


def duplicate_loan_ids(loans: list[LoanFacts]) -> TapeCheck:
    counts = Counter(loan.loan_id for loan in loans)
    dupes = sorted(k for k, v in counts.items() if v > 1)
    check = TapeCheck(
        "I1",
        "Loan IDs are unique across the tape",
        Status.PASS.value,
        Severity.CRITICAL.value,
        f"{len(counts)} distinct Loan IDs, none repeated",
        {"distinct_loan_ids": len(counts)},
    )
    if dupes:
        check.status = Status.FAIL.value
        check.message = f"{len(dupes)} Loan ID(s) appear more than once in the tape: {dupes[:5]}"
        check.evidence = {"duplicate_loan_ids": dupes[:50], "count": len(dupes)}
    return check


def borrower_demographics_consistent(loans: list[LoanFacts]) -> TapeCheck:
    by_borrower: dict[str, set[str | None]] = {}
    for loan in loans:
        if loan.borrower_token is None:
            continue
        by_borrower.setdefault(loan.borrower_token, set()).add(loan.demographics_digest)
    loans_per_borrower = Counter(loan.borrower_token for loan in loans if loan.borrower_token)
    repeat = sum(1 for n in loans_per_borrower.values() if n > 1)
    conflicting = sorted(b for b, keys in by_borrower.items() if len(keys) > 1)
    check = TapeCheck(
        "I2",
        "Same borrower carries the same demographics on every loan",
        Status.PASS.value,
        Severity.HIGH.value,
        f"{len(by_borrower)} borrower(s) checked, no conflicting birth year / borrower type",
        {"borrowers": len(by_borrower), "borrowers_with_several_loans": repeat},
    )
    if not by_borrower:
        check.status = Status.NOT_EVALUABLE.value
        check.message = "no Borrower ID values available"
    elif conflicting:
        check.status = Status.FAIL.value
        check.message = (
            f"{len(conflicting)} borrower(s) have conflicting demographics across their loans"
        )
        check.evidence = {"count": len(conflicting)}
    return check


def circuit_breaker(results: list[LoanResult], config: Config) -> TapeCheck:
    limit = config.tape.circuit_breaker_share
    check = TapeCheck(
        "I3",
        "Share of loans with a high/critical data-integrity defect stays below the limit",
        Status.NOT_EVALUABLE.value,
        Severity.CRITICAL.value,
        "no loans evaluated",
        {"limit": limit},
    )
    if not results:
        return check
    # Integrity-axis severity only: a high credit event on a loan with a low-grade data defect
    # must not count as a severe integrity defect.
    severe_ranks = (Severity.HIGH.value, Severity.CRITICAL.value)
    severe = sum(
        1
        for r in results
        if r.data_integrity == "defect"
        and any(
            f.root_cause and f.axis == "integrity" and f.severity in severe_ranks
            for f in r.findings
        )
    )
    share = severe / len(results)
    check.evidence = {
        "severe_defect_share": round(share, 3),
        "severe_defects": severe,
        "limit": limit,
    }
    if share <= limit:
        check.status = Status.PASS.value
        check.message = (
            f"{severe} of {len(results)} loans ({share:.0%}) carry a high/critical data-integrity "
            f"defect (limit {limit:.0%})"
        )
    else:
        check.status = Status.FAIL.value
        check.message = (
            f"{share:.0%} of loans carry a high/critical data-integrity defect (limit "
            f"{limit:.0%}); the tape format itself is suspect - hold publication and review the "
            "lender export"
        )
    return check


def _mad(counts: Counter[int], expected: dict[int, float]) -> float:
    n = sum(counts.values())
    return sum(abs(counts.get(d, 0) / n - p) for d, p in expected.items()) / len(expected)


def _band(mad: float, two_digits: bool) -> str:
    for one, two, name in MAD_BANDS:
        if mad <= (two if two_digits else one):
            return name
    return "nonconformity"


def _digit_profile(cents: list[int]) -> dict[str, object]:
    """First-digit, first-two-digit and summation statistics of one population of amounts."""
    first = Counter(_first_digit(c) for c in cents)
    two = Counter(int(str(c // 100)[:2]) for c in cents if c >= 1000)
    sums: dict[int, int] = {d: 0 for d in range(1, 10)}
    for c in cents:
        sums[_first_digit(c)] += c
    total = sum(sums.values())
    out: dict[str, object] = {
        "distinct_amounts": len(cents),
        "first_digit_mad": round(_mad(first, BENFORD), 4),
        "first_digit_band": _band(_mad(first, BENFORD), two_digits=False),
        "observed_share": {str(d): round(first.get(d, 0) / len(cents), 3) for d in range(1, 10)},
        "summation_share": {str(d): round(sums[d] / total, 3) for d in range(1, 10)},
    }
    if len(two) >= 10:
        out["first_two_digits_mad"] = round(_mad(two, BENFORD_TWO), 4)
        out["first_two_digits_band"] = _band(_mad(two, BENFORD_TWO), two_digits=True)
    return out


def benford_first_digit(loans: list[LoanFacts], config: Config) -> TapeCheck:
    """Information only: the *distinct* paid amounts of the tape should roughly follow Benford.

    Three refinements over a naive first-digit count: (1) each amount counts once per loan,
    because an annuity's repeated instalment is one number, not one sample per month;
    (2) the first-two-digit test and Nigrini's summation test (the share of total value per
    first digit, which exposes a few very large fabricated numbers a count cannot see) are
    reported alongside, with the loan that dominates the heaviest digit; (3) the verdict needs
    enough *loans*, not just enough rows - the amounts of a tape are seeded by its loans'
    (principal, rate, term) triples, so a small tape deviates from Benford for arithmetic
    reasons. Below ``benford_min_loans`` the statistics are reported, no verdict is given.
    Segments (status, product, vintage) are profiled so a deviation can be localised.
    """
    tape = config.tape
    cents = [c for loan in loans for c in loan.paid_cents if c >= 100]
    contributing = sum(1 for loan in loans if any(c >= 100 for c in loan.paid_cents))
    check = TapeCheck(
        "I4",
        "Distinct paid amounts follow Benford's law (bulk-edit / fabrication screen)",
        Status.NOT_EVALUABLE.value,
        Severity.INFO.value,
        f"only {len(cents)} distinct paid amounts; need {tape.benford_min_sample}",
        {"sample": len(cents), "min_sample": tape.benford_min_sample, "loans": contributing},
    )
    if len(cents) < tape.benford_min_sample:
        return check
    span = math.log10(max(cents) / min(cents))
    profile = _digit_profile(cents)
    heaviest = max(range(1, 10), key=lambda d: sum(c for c in cents if _first_digit(c) == d))
    top_loan = max(
        loans,
        key=lambda ln: sum(c for c in ln.paid_cents if c >= 100 and _first_digit(c) == heaviest),
    )
    segments: dict[str, dict[str, object]] = {}
    keys: list[tuple[str, Callable[[LoanFacts], object]]] = [
        ("status", lambda ln: ln.loan_status),
        ("loan_type", lambda ln: ln.loan_type),
        ("vintage", lambda ln: ln.vintage),
    ]
    for label, key in keys:
        groups: dict[str, list[int]] = {}
        members: Counter[str] = Counter()
        for ln in loans:
            k = str(key(ln))
            groups.setdefault(k, []).extend(c for c in ln.paid_cents if c >= 100)
            members[k] += 1
        for k, vals in sorted(groups.items()):
            if len(vals) >= tape.benford_min_sample:
                seg = _digit_profile(vals)
                seg["loans"] = members[k]
                segments[f"{label}={k}"] = seg
    check.evidence = {
        **profile,
        "sample": len(cents),
        "paid_rows": sum(len(ln.paid_rows) for ln in loans),
        "loans": contributing,
        "min_loans": tape.benford_min_loans,
        "orders_of_magnitude": round(span, 2),
        "max_mad": tape.benford_max_mad,
        "heaviest_digit_by_value": {
            "digit": heaviest,
            "share_of_total": profile["summation_share"][str(heaviest)],  # type: ignore[index]
            "top_loan": top_loan.loan_id,
        },
        "segments": segments,
    }
    mad = float(profile["first_digit_mad"])  # type: ignore[arg-type]
    summary = (
        f"{len(cents)} distinct paid amounts from {contributing} loans: first-digit MAD {mad:.3f} "
        f"({profile['first_digit_band']})"
    )
    if "first_two_digits_mad" in profile:
        summary += (
            f", first-two-digit MAD {profile['first_two_digits_mad']} "
            f"({profile['first_two_digits_band']})"
        )
    if contributing < tape.benford_min_loans:
        check.message = (
            f"{summary}; {contributing} loans seed too few independent amounts for a verdict "
            f"(need {tape.benford_min_loans}) - reported for information"
        )
    elif span < 2:
        check.message = (
            f"{summary}; amounts span only {span:.1f} orders of magnitude, Benford does not apply"
        )
    elif mad <= tape.benford_max_mad:
        check.status = Status.PASS.value
        check.message = f"{summary} - within tolerance {tape.benford_max_mad}"
    else:
        check.status = Status.FAIL.value
        check.message = (
            f"{summary} - deviates from Benford's law (tolerance {tape.benford_max_mad}); "
            f"digit {heaviest} carries {profile['summation_share'][str(heaviest)]:.0%} of the "  # type: ignore[index]
            f"value, led by loan {top_loan.loan_id}; see segments for where it sits"
        )
    return check


def shared_diary_rows(loans: list[LoanFacts], config: Config) -> TapeCheck:
    """A paid diary row (due date, paid date, type, amount) that appears under two loans is a
    copy, not a coincidence: two borrowers do not pay the same cents on the same two dates for
    the same reason. Nigrini's number-duplication test applied at row granularity so that it
    names the loans instead of reporting a statistic."""
    owners: dict[str, set[int]] = {}
    for loan in loans:
        for key in loan.paid_rows:
            owners.setdefault(key, set()).add(loan.loan_id)
    rows = sum(len(loan.paid_rows) for loan in loans)
    shared = {k: sorted(v) for k, v in owners.items() if len(v) > 1}
    floor = _cents(config.tape.duplicate_amount_floor_eur)
    amount_owners: dict[int, set[int]] = {}
    for loan in loans:
        for c in loan.paid_cents:
            if c >= floor and c % 100:
                amount_owners.setdefault(c, set()).add(loan.loan_id)
    shared_amounts = {c: sorted(v) for c, v in amount_owners.items() if len(v) > 1}
    check = TapeCheck(
        "I5",
        "No paid diary row is shared by two loans (copy / padding screen)",
        Status.PASS.value,
        Severity.HIGH.value,
        f"{rows} paid rows across {len(loans)} loans, none repeated under another loan",
        {
            "paid_rows": rows,
            "shared_rows": 0,
            "shared_amounts_over_floor": len(shared_amounts),
            "amount_floor_eur": config.tape.duplicate_amount_floor_eur,
            "shared_amount_examples": [
                {"amount": c / 100, "loans": ids}
                for c, ids in sorted(shared_amounts.items(), key=lambda kv: -len(kv[1]))[:10]
            ],
        },
    )
    if not rows:
        check.status = Status.NOT_EVALUABLE.value
        check.message = "no paid diary rows to compare"
    elif shared:
        loans_hit = sorted({i for ids in shared.values() for i in ids})
        check.status = Status.FAIL.value
        check.message = (
            f"{len(shared)} paid diary row(s) appear under more than one loan "
            f"({len(loans_hit)} loans involved, e.g. {loans_hit[:6]}): rows copied between loans"
        )
        check.evidence.update(
            {
                "shared_rows": len(shared),
                "loans": loans_hit[:50],
                "examples": [{"row": k, "loans": ids} for k, ids in sorted(shared.items())[:20]],
            }
        )
    return check


def round_number_screen(loans: list[LoanFacts], config: Config) -> TapeCheck:
    """Amounts nobody chose - interest, settlements, pending balances, summary balances -
    have cents that are effectively random, so about 1 % end in .00. A tape where a large
    share does was typed or generated by hand. ``Days late`` clustering on multiples of 30 is
    the same signature on the time axis."""
    tape = config.tape
    free = [c for loan in loans for c in loan.free_cents if c >= 100]
    round_share = sum(1 for c in free if c % 100 == 0) / len(free) if free else None
    late = [loan.days_late for loan in loans if loan.days_late]
    late_share = sum(1 for d in late if d % 30 == 0) / len(late) if late else None
    check = TapeCheck(
        "I6",
        "Cents of computed amounts look random (hand-entry / generation screen)",
        Status.NOT_EVALUABLE.value,
        Severity.INFO.value,
        f"only {len(free)} computed amounts; need {tape.round_min_sample}",
        {
            "computed_amounts": len(free),
            "round_share": None if round_share is None else round(round_share, 3),
            "max_round_share": tape.round_share_max,
            "days_late_values": len(late),
            "days_late_multiple_of_30_share": None if late_share is None else round(late_share, 3),
        },
    )
    if len(free) < tape.round_min_sample:
        return check
    assert round_share is not None
    late_note = (
        f"; {late_share:.0%} of {len(late)} non-zero Days late values are multiples of 30"
        if late_share is not None and len(late) >= tape.round_min_days_late
        else ""
    )
    if round_share <= tape.round_share_max:
        check.status = Status.PASS.value
        check.message = (
            f"{round_share:.1%} of {len(free)} computed amounts end in .00 "
            f"(limit {tape.round_share_max:.0%}){late_note}"
        )
    else:
        check.status = Status.FAIL.value
        check.message = (
            f"{round_share:.1%} of {len(free)} computed amounts end in .00 (limit "
            f"{tape.round_share_max:.0%}): interest, settlements or balances were rounded or "
            f"typed by hand{late_note}"
        )
    return check


def evaluate_tape(
    loans: list[Loan], results: list[LoanResult], config: Config
) -> tuple[list[TapeCheck], list[TapeFinding]]:
    return evaluate_tape_facts(facts_of(loans, lender_scope=config.lender), results, config)


def evaluate_tape_facts(
    facts: list[LoanFacts], results: list[LoanResult], config: Config
) -> tuple[list[TapeCheck], list[TapeFinding]]:
    checks = [
        duplicate_loan_ids(facts),
        borrower_demographics_consistent(facts),
        circuit_breaker(results, config),
        benford_first_digit(facts, config),
        shared_diary_rows(facts, config),
        round_number_screen(facts, config),
    ]
    findings = [
        TapeFinding(c.rule_id, c.severity, c.message, c.evidence)
        for c in checks
        if c.status == Status.FAIL.value
    ]
    return checks, findings


def flagged_population_profile(results: list[LoanResult]) -> dict[str, object]:
    """Descriptive breakdown of the flagged loans - context for a reader, never a verdict."""
    flagged = [r for r in results if r.verdict == "flagged"]
    by_status: Counter[str] = Counter(
        public_category("Loan status", r.loan_status) or "unknown" for r in flagged
    )
    by_axis: Counter[str] = Counter()
    by_root_rule: Counter[str] = Counter()
    for r in flagged:
        axes = {f.axis for f in r.findings if f.root_cause and f.severity != "info"}
        by_axis["+".join(sorted(axes)) or "none"] += 1
        for f in r.findings:
            if f.root_cause and f.severity != "info":
                by_root_rule[f.rule_id] += 1
    return {
        "flagged": len(flagged),
        "flagged_share": round(len(flagged) / len(results), 3) if results else None,
        "by_loan_status": dict(sorted(by_status.items())),
        "by_root_axis": dict(sorted(by_axis.items())),
        "by_root_rule": dict(sorted(by_root_rule.items())),
        "normal_with_minor_observations": sum(
            1
            for r in results
            if r.verdict == "normal" and any(f.severity != "info" for f in r.findings)
        ),
    }
