"""Category A - can the row be read, and does it use the agreed vocabulary?"""

from __future__ import annotations

import re

from loan_dq.ingest.model import PaymentState, PaymentType
from loan_dq.rules.base import Axis, BaseRule, LoanContext, Outcome, Severity

NUMERIC_FIELDS = {
    "Loan amount",
    "Interest rate",
    "Loan term",
    "Monthly payment",
    "Days late",
    "Outstanding principal",
    "Repaid principal",
    "Outstanding interest",
    "Repaid interest",
    "Arrears",
    "Delay interest",
}
DATE_FIELDS = {"Disbursal date", "Expected repayment date", "Repayment date"}


class A2BorrowerIdPresent(BaseRule):
    id = "A2"
    category = "readability"
    severity = Severity.HIGH
    title = "Borrower ID present"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        if "Borrower ID" in ctx.loan.missing_optional_columns:
            return Outcome.skip("Borrower ID column absent")
        if ctx.loan.borrower_id is None:
            return Outcome.fail("Borrower ID is missing", {"field": "Borrower ID"})
        return Outcome.ok()


class A3NumericFieldsParse(BaseRule):
    id = "A3"
    category = "readability"
    severity = Severity.HIGH
    title = "Numeric summary fields are numbers"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        bad = [i for i in ctx.loan.parse_issues if i.field in NUMERIC_FIELDS]
        if not bad:
            return Outcome.ok()
        fields = sorted({i.field for i in bad})
        return Outcome.fail(
            f"{len(fields)} numeric field(s) could not be parsed: {', '.join(fields)}",
            {"fields": fields, "problems": [i.problem for i in bad][:5]},
        )


class A4DateFieldsParse(BaseRule):
    id = "A4"
    category = "readability"
    severity = Severity.HIGH
    title = "Date summary fields parse"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        bad = [i for i in ctx.loan.parse_issues if i.field in DATE_FIELDS]
        if not bad:
            return Outcome.ok()
        fields = sorted({i.field for i in bad})
        return Outcome.fail(
            f"{len(fields)} date field(s) could not be parsed: {', '.join(fields)}",
            {"fields": fields, "problems": [i.problem for i in bad][:5]},
        )


class A5ConsistentDateFormats(BaseRule):
    id = "A5"
    category = "readability"
    severity = Severity.MEDIUM
    title = "Diary uses one date format"
    subsumed_by = ("A7",)

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        formats = {f for p in ctx.diary for f in p.date_formats}
        if len(formats) <= 1:
            return Outcome.ok()
        return Outcome.fail(
            f"payment diary mixes date formats {sorted(formats)}", {"formats": sorted(formats)}
        )


class A6ControlledVocabulary(BaseRule):
    id = "A6"
    category = "readability"
    severity = Severity.MEDIUM
    title = "Categorical fields use known values"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        s = ctx.config.schema_
        loan = ctx.loan
        problems: dict[str, object] = {}
        checks: list[tuple[str, str | None, list[str], bool]] = [
            ("Loan status", loan.loan_status, s.loan_status_values, True),
            ("Loan type", loan.loan_type, s.loan_type_values, False),
            ("Borrower type", loan.borrower_type, s.borrower_type_values, False),
            ("Credit score", loan.credit_score, s.credit_score_values, False),
        ]
        for name, value, allowed, required in checks:
            if name in loan.missing_optional_columns:
                continue
            if value is None:
                if required:
                    problems[name] = "missing"
                continue
            if value not in allowed:
                problems[name] = value
        untranslated = loan.purpose is not None and bool(
            re.match(s.purpose_code_pattern, loan.purpose)
        )
        if not problems and not untranslated:
            return Outcome.ok()
        if not problems:
            return Outcome.fail(
                f"Purpose holds an untranslated enum code {loan.purpose!r} instead of a label",
                {"fields": {"Purpose": loan.purpose}},
                severity=Severity.LOW,
            )
        if untranslated:
            problems["Purpose"] = loan.purpose
        return Outcome.fail(
            "unexpected categorical value(s): "
            + ", ".join(f"{k}={v!r}" for k, v in problems.items()),
            {"fields": problems},
        )


class A7DiaryReadable(BaseRule):
    id = "A7"
    category = "readability"
    severity = Severity.HIGH
    title = "Payment diary parses"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        d = ctx.loan.diary
        if d.parse_error:
            return Outcome.fail(
                f"payment diary unreadable: {d.parse_error}",
                {"raw_length": d.raw_length, "error": d.parse_error},
            )
        if not d.payments:
            return Outcome.fail("payment diary is empty", {"raw_length": d.raw_length})
        return Outcome.ok()


class A8DiaryComplete(BaseRule):
    id = "A8"
    category = "readability"
    severity = Severity.INFO
    axis = Axis.COVERAGE
    title = "Payment diary not truncated"
    subsumed_by = ("A7",)

    def evaluate(self, ctx: LoanContext) -> Outcome:
        d = ctx.loan.diary
        if not d.readable:
            return Outcome.skip("diary unreadable")
        if not d.truncated:
            return Outcome.ok()
        return Outcome.fail(
            f"payment diary truncated at {d.raw_length} characters (Excel cell limit); "
            f"{len(d.payments)} complete records salvaged, sum-based checks not evaluated",
            {
                "raw_length": d.raw_length,
                "records_salvaged": len(d.payments),
                "dropped_tail_chars": d.dropped_tail_chars,
            },
        )


class A9PaymentRecordsWellFormed(BaseRule):
    id = "A9"
    category = "readability"
    severity = Severity.MEDIUM
    title = "Payment records have expected keys, types and states"
    subsumed_by = ("A7",)

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        bad = [p for p in ctx.diary if p.issues]
        if not bad:
            return Outcome.ok()
        problems = sorted({f"{i.field}: {i.problem}" for p in bad for i in p.issues})
        unknown_types = sorted({p.type_raw for p in bad if p.type is PaymentType.UNKNOWN})
        unknown_states = sorted({p.state_raw for p in bad if p.state is PaymentState.UNKNOWN})
        return Outcome.fail(
            f"{len(bad)} of {len(ctx.diary)} payment records malformed: {problems[0]}"
            + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""),
            {
                "bad_records": len(bad),
                "problems": problems[:8],
                "unknown_types": unknown_types,
                "unknown_states": unknown_states,
            },
        )


class A10DiaryLoanIdMatches(BaseRule):
    id = "A10"
    category = "readability"
    severity = Severity.HIGH
    title = "Every diary record belongs to this loan"
    subsumed_by = ("A7",)

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        expected = str(ctx.loan.loan_id)
        foreign = sorted(
            {p.loan_id_raw for p in ctx.diary if p.loan_id_raw not in (None, expected)}
        )
        if not foreign:
            return Outcome.ok()
        return Outcome.fail(
            f"payment diary contains records for other loan ID(s): {foreign[:3]}",
            {"expected": expected, "found": [f for f in foreign if f is not None][:10]},
        )


RULES = [
    A2BorrowerIdPresent(),
    A3NumericFieldsParse(),
    A4DateFieldsParse(),
    A5ConsistentDateFormats(),
    A6ControlledVocabulary(),
    A7DiaryReadable(),
    A8DiaryComplete(),
    A9PaymentRecordsWellFormed(),
    A10DiaryLoanIdMatches(),
]
