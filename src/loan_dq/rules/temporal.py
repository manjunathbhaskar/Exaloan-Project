"""Category D - dates must be possible and the schedule must be regular."""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

from loan_dq.ingest.model import PaymentState, PaymentType
from loan_dq.rules.base import BaseRule, LoanContext, Outcome, Severity


def _readings(ctx: LoanContext, column: str, value: date | None) -> list[date]:
    if value is None:
        return []
    return ctx.loan.ambiguous_dates.get(column) or [value]


class D1NoPaymentBeforeDisbursal(BaseRule):
    id = "D1"
    category = "temporal"
    severity = Severity.HIGH
    title = "No payment dated before disbursal"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        disbursal = ctx.loan.disbursal_date
        if disbursal is None:
            return Outcome.skip("Disbursal date missing")
        readings = _readings(ctx, "Disbursal date", disbursal)
        worst: list[tuple[str, date]] = []
        for p in ctx.diary:
            for label, d in (("due", p.due_date), ("paid", p.actual_date)):
                if d is not None and all(d < r for r in readings):
                    worst.append((label, d))
        if not worst:
            return Outcome.ok()
        label, d = min(worst, key=lambda x: x[1])
        return Outcome.fail(
            f"{len(worst)} payment date(s) precede the disbursal date {disbursal.isoformat()} "
            f"(earliest: {label} {d.isoformat()})",
            {
                "disbursal_date": disbursal.isoformat(),
                "earliest": d.isoformat(),
                "count": len(worst),
            },
        )


class D2NoFutureDatedPayments(BaseRule):
    id = "D2"
    category = "temporal"
    severity = Severity.HIGH
    title = "No paid instalment dated after the as-of date"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        future = [
            p
            for p in ctx.diary
            if p.state.is_paid and p.actual_date is not None and p.actual_date > ctx.as_of
        ]
        if not future:
            return Outcome.ok()
        latest = max(p.actual_date for p in future if p.actual_date)
        return Outcome.fail(
            f"{len(future)} row(s) marked paid on a date after the as-of date "
            f"{ctx.as_of.isoformat()} (latest {latest.isoformat()})",
            {
                "as_of": ctx.as_of.isoformat(),
                "latest_paid": latest.isoformat(),
                "count": len(future),
            },
        )


class D3ScheduleIsRegular(BaseRule):
    id = "D3"
    category = "temporal"
    severity = Severity.MEDIUM
    title = "Principal due dates are roughly monthly with no gaps or doubles"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        dues = sorted(
            {p.due_date for p in ctx.regular if p.type is PaymentType.PRINCIPAL and p.due_date}
        )
        if len(dues) < 3:
            return Outcome.skip("fewer than three scheduled principal dates")
        max_gap = ctx.config.tolerance.schedule_gap_days
        min_gap = ctx.config.tolerance.schedule_min_gap_days
        gaps: list[dict[str, object]] = []
        doubles: list[dict[str, object]] = []
        for a, b in pairwise(dues):
            days = (b - a).days
            if days > max_gap:
                gaps.append({"from": a.isoformat(), "to": b.isoformat(), "days": days})
            elif days < min_gap:
                doubles.append({"from": a.isoformat(), "to": b.isoformat(), "days": days})
        if not gaps and not doubles:
            return Outcome.ok()
        parts = []
        if gaps:
            g = gaps[0]
            parts.append(
                f"{len(gaps)} gap(s) in the schedule "
                f"(e.g. {g['from']} -> {g['to']} = {g['days']} days)"
            )
        if doubles:
            d = doubles[0]
            parts.append(
                f"{len(doubles)} pair(s) of due dates only {d['days']} days apart "
                f"({d['from']} / {d['to']})"
            )
        return Outcome.fail("; ".join(parts), {"gaps": gaps[:3], "doubles": doubles[:3]})


class D4LastPaymentNotAfterRepaymentDate(BaseRule):
    id = "D4"
    category = "temporal"
    severity = Severity.MEDIUM
    title = "Last actual payment is not after the loan's Repayment date"
    subsumed_by = ("E1",)

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        loan = ctx.loan
        if loan.repayment_date is None:
            return Outcome.skip("Repayment date missing")
        paid_dates = [p.actual_date for p in ctx.diary if p.state.is_paid and p.actual_date]
        if not paid_dates:
            return Outcome.skip("no paid rows")
        last = max(paid_dates)
        slack = timedelta(days=ctx.config.tolerance.repayment_date_slack_days)
        if any(last <= r + slack for r in _readings(ctx, "Repayment date", loan.repayment_date)):
            return Outcome.ok()
        return Outcome.fail(
            f"last payment received {last.isoformat()} is after the loan Repayment date "
            f"{loan.repayment_date.isoformat()}",
            {"last_paid": last.isoformat(), "repayment_date": loan.repayment_date.isoformat()},
        )


class D5LoanDatesOrdered(BaseRule):
    id = "D5"
    category = "temporal"
    severity = Severity.HIGH
    title = "Repayment / expected repayment dates are not before disbursal"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        if loan.disbursal_date is None:
            return Outcome.skip("Disbursal date missing")
        disbursal_readings = _readings(ctx, "Disbursal date", loan.disbursal_date)
        problems: dict[str, str] = {}
        for column, value in (
            ("Repayment date", loan.repayment_date),
            ("Expected repayment date", loan.expected_repayment_date),
        ):
            readings = _readings(ctx, column, value)
            if readings and all(v < d for v in readings for d in disbursal_readings):
                assert value is not None
                problems[column] = value.isoformat()
        if not problems:
            return Outcome.ok()
        return Outcome.fail(
            "; ".join(
                f"{k} {v} is before disbursal {loan.disbursal_date.isoformat()}"
                for k, v in problems.items()
            ),
            {"disbursal_date": loan.disbursal_date.isoformat(), **problems},
        )


class D6GraceRowsRecent(BaseRule):
    id = "D6"
    category = "temporal"
    severity = Severity.MEDIUM
    title = "'payment in grace period' rows are within the grace window"
    subsumed_by = ("E1",)

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        limit = ctx.config.tolerance.grace_state_days
        stale = [
            (p, dpd)
            for p in ctx.diary
            if p.state is PaymentState.GRACE
            and (dpd := ctx.days_past_due(p)) is not None
            and dpd > limit
        ]
        if not stale:
            return Outcome.ok()
        p, dpd = max(stale, key=lambda x: x[1])
        assert p.due_date is not None
        return Outcome.fail(
            f"{len(stale)} row(s) still 'in grace period' although {dpd} days past due "
            f"(due {p.due_date.isoformat()}, grace window {limit} days)",
            {"rows": len(stale), "oldest_due": p.due_date.isoformat(), "days_past_due": dpd},
        )


class D7NoUnexplainedEarlyPayments(BaseRule):
    id = "D7"
    category = "temporal"
    severity = Severity.LOW
    title = "Payments before due date are explained by an early-repayment event"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        early = [
            p
            for p in ctx.lateness_rows
            if p.state.is_paid and (d := p.delay_days) is not None and d < -3
        ]
        if not early:
            return Outcome.ok()
        p = min(early, key=lambda x: x.delay_days or 0)
        assert p.due_date and p.actual_date
        return Outcome.fail(
            f"{len(early)} instalment(s) paid well before due date without an early-repayment "
            f"event (e.g. due {p.due_date.isoformat()}, paid {p.actual_date.isoformat()})",
            {
                "rows": len(early),
                "example_due": p.due_date.isoformat(),
                "example_paid": p.actual_date.isoformat(),
            },
        )


class D8OnTimeLabelWithinGrace(BaseRule):
    id = "D8"
    category = "temporal"
    severity = Severity.LOW
    title = "'paid on time' rows were paid within the lender's grace window"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        grace = ctx.config.lateness.grace_days
        mislabelled = [
            (p, d)
            for p in ctx.lateness_rows
            if p.state is PaymentState.PAID_ON_TIME
            and not p.is_closure_row
            and (d := p.delay_days) is not None
            and d > grace
        ]
        if not mislabelled:
            return Outcome.ok()
        p, d = max(mislabelled, key=lambda x: x[1])
        assert p.due_date and p.actual_date
        return Outcome.fail(
            f"{len(mislabelled)} row(s) labelled 'paid on time' but paid more than {grace} days "
            f"after due (worst: due {p.due_date.isoformat()}, paid {p.actual_date.isoformat()}, "
            f"{d} days)",
            {
                "rows": len(mislabelled),
                "worst_due": p.due_date.isoformat(),
                "worst_paid": p.actual_date.isoformat(),
                "worst_delay_days": d,
                "grace_days": grace,
            },
        )


RULES = [
    D1NoPaymentBeforeDisbursal(),
    D2NoFutureDatedPayments(),
    D3ScheduleIsRegular(),
    D4LastPaymentNotAfterRepaymentDate(),
    D5LoanDatesOrdered(),
    D6GraceRowsRecent(),
    D7NoUnexplainedEarlyPayments(),
    D8OnTimeLabelWithinGrace(),
]
