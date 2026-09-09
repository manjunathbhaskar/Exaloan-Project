"""Category F - the brief's headline check: 90+ days between scheduled and actual payment.

Two distinct facts are measured, both against the same threshold:

* F1  a payment component that *was* paid, but >= N days after its due date (historical
      default);
* F2  a payment component that is *still unpaid* and >= N days past due at the as-of date
      (current default - the loan that simply stopped paying).

The diary stores one row per component (principal, interest, fee) of an instalment, so
messages count components and, separately, the distinct due dates they belong to.

F3 covers the 30-89 day band. All three sit on the credit axis: they describe borrower
behaviour honestly recorded in the tape, not a defect in the tape itself.
"""

from __future__ import annotations

from loan_dq.rules.base import (
    Axis,
    BaseRule,
    LoanContext,
    Outcome,
    Severity,
    due_dates,
    money,
    pending_portion,
)


class F1PaidLateBeyondDefault(BaseRule):
    id = "F1"
    category = "lateness"
    severity = Severity.HIGH
    axis = Axis.CREDIT
    title = "Payment component paid 90+ days after its scheduled date"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        threshold = ctx.config.lateness.default_days
        late = [(p, d) for p, d in ctx.paid_delays if d >= threshold]
        if not late:
            return Outcome.ok()
        worst, days = max(late, key=lambda x: x[1])
        assert worst.due_date and worst.actual_date
        severity = Severity.CRITICAL if ctx.loan.is_live else Severity.HIGH
        n_due = due_dates([p for p, _ in late])
        return Outcome.fail(
            f"{len(late)} payment component(s) across {n_due} due date(s) paid {threshold}+ days "
            f"late; worst: due {worst.due_date.isoformat()}, paid {worst.actual_date.isoformat()} "
            f"({days} days after scheduled date)",
            {
                "components_90_plus": len(late),
                "due_dates_90_plus": n_due,
                "worst_due": worst.due_date.isoformat(),
                "worst_paid": worst.actual_date.isoformat(),
                "worst_delay_days": days,
                "amount": money(worst.amount),
                "threshold_days": threshold,
            },
            severity=severity,
        )


class F2UnpaidBeyondDefault(BaseRule):
    id = "F2"
    category = "lateness"
    severity = Severity.CRITICAL
    axis = Axis.CREDIT
    title = "Payment component unpaid and 90+ days past due at the as-of date"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        threshold = ctx.config.lateness.default_days
        overdue = [
            (p, dpd)
            for p in ctx.unpaid_rows
            if (dpd := ctx.days_past_due(p)) is not None and dpd >= threshold
        ]
        if not overdue:
            return Outcome.ok()
        oldest, dpd = max(overdue, key=lambda x: x[1])
        assert oldest.due_date is not None
        # Still owed, not scheduled: a part-paid row contributes only its pending remainder.
        amount = sum(pending_portion(p) for p, _ in overdue)
        n_due = due_dates([p for p, _ in overdue])
        return Outcome.fail(
            f"{len(overdue)} payment component(s) across {n_due} due date(s) with EUR "
            f"{amount:,.2f} still unpaid and {threshold}+ days past due; oldest due "
            f"{oldest.due_date.isoformat()} is {dpd} days overdue at {ctx.as_of.isoformat()}",
            {
                "components_overdue": len(overdue),
                "due_dates_overdue": n_due,
                "overdue_amount": money(amount),
                "oldest_due": oldest.due_date.isoformat(),
                "oldest_days_past_due": dpd,
                "as_of": ctx.as_of.isoformat(),
                "threshold_days": threshold,
                "loan_status": ctx.loan.loan_status,
            },
        )


class F3LateWithinWarningBand(BaseRule):
    id = "F3"
    category = "lateness"
    severity = Severity.MEDIUM
    axis = Axis.CREDIT
    title = "Payment component 30-89 days late (paid or still unpaid)"
    subsumed_by = ("F1", "F2")

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        lo = ctx.config.lateness.medium_days
        hi = ctx.config.lateness.default_days
        paid_band = [(p, d) for p, d in ctx.paid_delays if lo <= d < hi]
        unpaid_band = [
            (p, dpd)
            for p in ctx.unpaid_rows
            if (dpd := ctx.days_past_due(p)) is not None and lo <= dpd < hi
        ]
        if not paid_band and not unpaid_band:
            return Outcome.ok()
        worst = max([d for _, d in paid_band] + [d for _, d in unpaid_band])
        # Arrears still open at as_of, or a paid delay at/above watch_flag_days, are a medium
        # credit event; shorter delays that were caught up are a low behaviour note.
        severity = (
            Severity.MEDIUM
            if unpaid_band or worst >= ctx.config.lateness.watch_flag_days
            else Severity.LOW
        )
        parts = []
        evidence: dict[str, object] = {"band_days": [lo, hi - 1], "worst_days": worst}
        if paid_band:
            p, d = max(paid_band, key=lambda x: x[1])
            assert p.due_date and p.actual_date
            parts.append(
                f"{len(paid_band)} payment component(s) across "
                f"{due_dates([q for q, _ in paid_band])} due date(s) paid {lo}-{hi - 1} days late "
                f"(worst: due {p.due_date.isoformat()}, paid {p.actual_date.isoformat()}, {d} days)"
            )
            evidence.update({"paid_late_count": len(paid_band), "paid_worst_days": d})
        if unpaid_band:
            p, dpd = max(unpaid_band, key=lambda x: x[1])
            assert p.due_date
            parts.append(
                f"{len(unpaid_band)} payment component(s) unpaid and {dpd} days past due "
                f"(due {p.due_date.isoformat()})"
            )
            evidence.update({"unpaid_count": len(unpaid_band), "unpaid_worst_days": dpd})
        return Outcome.fail("; ".join(parts), evidence, severity=severity)


RULES = [F1PaidLateBeyondDefault(), F2UnpaidBeyondDefault(), F3LateWithinWarningBand()]
