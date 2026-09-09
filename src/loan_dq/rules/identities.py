"""Category B - the summary row and the payment diary describe the same loan, so they must
agree. Each rule compares one summary field with the diary quantity that should equal it.
"""

from __future__ import annotations

from loan_dq.ingest.model import PaymentType
from loan_dq.rules.base import (
    BaseRule,
    LoanContext,
    Outcome,
    Severity,
    money,
    paid_portion,
    pending_portion,
    total,
)


def _month_diff(later_year: int, later_month: int, year: int, month: int) -> int:
    return (later_year - year) * 12 + (later_month - month)


class B1ScheduleSumsToLoanAmount(BaseRule):
    id = "B1"
    category = "identity"
    severity = Severity.HIGH
    title = "Scheduled principal + settlements = Loan amount"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=True)
        if skip:
            return skip
        loan = ctx.loan
        if loan.loan_amount is None:
            return Outcome.skip("Loan amount missing")
        scheduled = total(ctx.schedule_rows)
        settled = total(ctx.settlement_cash)
        gap = scheduled + settled - loan.loan_amount
        tol = ctx.config.tolerance.amount_eur
        if abs(gap) <= tol:
            return Outcome.ok({"gap": money(gap)})
        material = abs(gap) > ctx.config.tolerance.principal_gap_material_share * loan.loan_amount
        # The summary's principal split is the second witness: if it also reports the full Loan
        # amount, the diary cannot account for the money; if it agrees with the diary, only the
        # Loan amount header is off.
        summary_total = None
        if loan.repaid_principal is not None and loan.outstanding_principal is not None:
            summary_total = loan.repaid_principal + loan.outstanding_principal
        summary_backs_diary = (
            summary_total is not None and abs(summary_total - (scheduled + settled)) <= tol
        )
        direction = "exceeds" if gap > 0 else "falls short of"
        msg = (
            f"diary principal EUR {scheduled + settled:,.2f} (scheduled EUR {scheduled:,.2f} + "
            f"settlements EUR {settled:,.2f}) {direction} Loan amount EUR "
            f"{loan.loan_amount:,.2f} by EUR {abs(gap):,.2f}"
        )
        if material:
            severity = Severity.HIGH
            msg += (
                "; early-repayment and settlement rows are already counted, so the difference "
                "is unexplained"
            )
        elif summary_total is None:
            severity = Severity.LOW
            msg += "; summary principal split is missing, so the gap cannot be corroborated"
        elif summary_backs_diary:
            severity = Severity.LOW
            msg += "; summary principal split agrees with the diary, so only the Loan amount is off"
        else:
            severity = Severity.MEDIUM
            msg += (
                f"; summary principal split (repaid + outstanding) also reports EUR "
                f"{summary_total:,.2f}, so EUR {abs(gap):,.2f} is not traceable to any diary row"
            )
        return Outcome.fail(
            msg,
            {
                "loan_amount": money(loan.loan_amount),
                "scheduled_principal": money(scheduled),
                "settlement_cash": money(settled),
                "summary_principal_total": money(summary_total),
                "gap": money(gap),
                "tolerance": tol,
            },
            severity=severity,
        )


class B2PrincipalSplitSumsToLoanAmount(BaseRule):
    id = "B2"
    category = "identity"
    severity = Severity.CRITICAL
    title = "Outstanding + Repaid principal = Loan amount"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        if None in (loan.loan_amount, loan.outstanding_principal, loan.repaid_principal):
            return Outcome.skip("Loan amount / Outstanding principal / Repaid principal missing")
        assert loan.loan_amount is not None
        assert loan.outstanding_principal is not None
        assert loan.repaid_principal is not None
        gap = loan.outstanding_principal + loan.repaid_principal - loan.loan_amount
        tol = ctx.config.tolerance.amount_eur
        if abs(gap) <= tol:
            return Outcome.ok({"gap": money(gap)})
        return Outcome.fail(
            f"Outstanding principal EUR {loan.outstanding_principal:,.2f} + Repaid principal "
            f"EUR {loan.repaid_principal:,.2f} differs from Loan amount "
            f"EUR {loan.loan_amount:,.2f} "
            f"by EUR {gap:+,.2f}",
            {
                "loan_amount": money(loan.loan_amount),
                "outstanding_principal": money(loan.outstanding_principal),
                "repaid_principal": money(loan.repaid_principal),
                "gap": money(gap),
                "tolerance": tol,
            },
        )


class B3PaidPrincipalMatchesRepaid(BaseRule):
    id = "B3"
    category = "identity"
    severity = Severity.HIGH
    title = "Paid principal in diary = Repaid principal"
    subsumed_by = ("E1", "B1")

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=True)
        if skip:
            return skip
        loan = ctx.loan
        if loan.repaid_principal is None:
            return Outcome.skip("Repaid principal missing")
        paid_schedule = sum(paid_portion(p) for p in ctx.schedule_rows)
        paid_settled = sum(paid_portion(p) for p in ctx.settlement_cash)
        diary_paid = paid_schedule + paid_settled
        gap = diary_paid - loan.repaid_principal
        tol = ctx.config.tolerance.amount_eur
        if abs(gap) <= tol:
            return Outcome.ok({"gap": money(gap)})
        return Outcome.fail(
            f"diary shows EUR {diary_paid:,.2f} principal paid but summary Repaid principal is "
            f"EUR {loan.repaid_principal:,.2f} (gap EUR {gap:+,.2f})",
            {
                "diary_paid_principal": money(diary_paid),
                "repaid_principal": money(loan.repaid_principal),
                "gap": money(gap),
                "tolerance": tol,
            },
        )


class B4PaidInterestMatchesRepaid(BaseRule):
    id = "B4"
    category = "identity"
    severity = Severity.HIGH
    title = "Paid interest in diary = Repaid interest"
    subsumed_by = ("E1",)

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=True)
        if skip:
            return skip
        loan = ctx.loan
        if "Repaid interest" in loan.missing_optional_columns:
            return Outcome.skip("Repaid interest column absent")
        summary = loan.repaid_interest or 0.0
        diary_paid = sum(
            paid_portion(p) for p in ctx.interest_rows if p.type is PaymentType.INTEREST
        )
        gap = diary_paid - summary
        tol = ctx.config.tolerance.interest_eur
        if abs(gap) <= tol:
            return Outcome.ok({"gap": money(gap)})
        return Outcome.fail(
            f"diary shows EUR {diary_paid:,.2f} interest paid but summary Repaid interest is "
            f"EUR {summary:,.2f} (gap EUR {gap:+,.2f})",
            {
                "diary_paid_interest": money(diary_paid),
                "repaid_interest": money(summary),
                "gap": money(gap),
                "tolerance": tol,
            },
        )


class B5PendingInterestMatchesOutstanding(BaseRule):
    id = "B5"
    category = "identity"
    severity = Severity.HIGH
    title = "Pending interest in diary = Outstanding interest"
    subsumed_by = ("E1", "B4")

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=True)
        if skip:
            return skip
        loan = ctx.loan
        if "Outstanding interest" in loan.missing_optional_columns:
            return Outcome.skip("Outstanding interest column absent")
        summary = loan.outstanding_interest or 0.0
        diary_pending = sum(
            pending_portion(p)
            for p in ctx.interest_rows
            if p.type is PaymentType.INTEREST and not p.is_closure_row
        )
        gap = diary_pending - summary
        tol = ctx.config.tolerance.interest_eur
        if abs(gap) <= tol:
            return Outcome.ok({"gap": money(gap)})
        return Outcome.fail(
            f"diary shows EUR {diary_pending:,.2f} interest still pending but summary Outstanding "
            f"interest is EUR {summary:,.2f} (gap EUR {gap:+,.2f})",
            {
                "diary_pending_interest": money(diary_pending),
                "outstanding_interest": money(summary),
                "gap": money(gap),
                "tolerance": tol,
            },
        )


class B6OverdueInterestMatchesDelayInterest(BaseRule):
    id = "B6"
    category = "identity"
    severity = Severity.MEDIUM
    title = "Paid overdue interest in diary = Delay interest"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=True)
        if skip:
            return skip
        loan = ctx.loan
        if "Delay interest" in loan.missing_optional_columns:
            return Outcome.skip("Delay interest column absent")
        summary = loan.delay_interest or 0.0
        diary_paid = sum(
            paid_portion(p) for p in ctx.interest_rows if p.type is PaymentType.OVERDUE_INTEREST
        )
        gap = diary_paid - summary
        tol = ctx.config.tolerance.interest_eur
        if abs(gap) <= tol:
            return Outcome.ok({"gap": money(gap)})
        return Outcome.fail(
            f"diary shows EUR {diary_paid:,.2f} overdue interest paid but summary Delay interest "
            f"is EUR {summary:,.2f} (gap EUR {gap:+,.2f})",
            {
                "diary_paid_overdue_interest": money(diary_paid),
                "delay_interest": money(summary),
                "gap": money(gap),
                "tolerance": tol,
            },
        )


class B7DaysLateMatchesDiary(BaseRule):
    id = "B7"
    category = "identity"
    severity = Severity.MEDIUM
    title = "Summary Days late = current days past due in diary"
    subsumed_by = ("F2", "E1")

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        loan = ctx.loan
        if loan.days_late is None:
            return Outcome.skip("Days late missing")
        overdue = [
            dpd for p in ctx.unpaid_rows if (dpd := ctx.days_past_due(p)) is not None and dpd > 0
        ]
        diary_dpd = max(overdue) if overdue else 0
        if loan.diary.truncated and not overdue:
            return Outcome.skip("diary truncated; cannot see whether an instalment is overdue")
        tol = ctx.config.tolerance.days_late_days
        # Once a loan is terminated the lender calls the whole debt: Days late is then counted
        # from the termination claim, not from the oldest missed instalment.
        claims = [
            dpd
            for p in ctx.diary
            if p.type is PaymentType.TERMINATION
            and pending_portion(p) > 0.0
            and (dpd := ctx.days_past_due(p)) is not None
        ]
        acceptable = [diary_dpd, *claims]
        if any(abs(v - loan.days_late) <= tol for v in acceptable):
            return Outcome.ok()
        return Outcome.fail(
            f"summary says {loan.days_late} days late but the oldest unpaid instalment is "
            f"{diary_dpd} days past due at {ctx.as_of.isoformat()}",
            {
                "days_late": loan.days_late,
                "diary_days_past_due": diary_dpd,
                "termination_claim_days_past_due": max(claims) if claims else None,
                "as_of": ctx.as_of.isoformat(),
                "tolerance": tol,
            },
        )


class B11ExpectedRepaymentMatchesTerm(BaseRule):
    id = "B11"
    category = "identity"
    severity = Severity.MEDIUM
    title = "Expected repayment date = Disbursal date + Loan term"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        if None in (loan.disbursal_date, loan.expected_repayment_date, loan.loan_term_months):
            return Outcome.skip("Disbursal date / Expected repayment date / Loan term missing")
        assert loan.disbursal_date and loan.expected_repayment_date and loan.loan_term_months
        candidates = ctx.loan.ambiguous_dates.get("Expected repayment date") or [
            loan.expected_repayment_date
        ]
        tol_months = max(1, round(ctx.config.tolerance.expected_repayment_days / 30))
        for candidate in candidates:
            months = _month_diff(
                candidate.year, candidate.month, loan.disbursal_date.year, loan.disbursal_date.month
            )
            if abs(months - loan.loan_term_months) <= tol_months:
                return Outcome.ok()
        months = _month_diff(
            loan.expected_repayment_date.year,
            loan.expected_repayment_date.month,
            loan.disbursal_date.year,
            loan.disbursal_date.month,
        )
        return Outcome.fail(
            f"Expected repayment date {loan.expected_repayment_date.isoformat()} is "
            f"{months} months "
            f"after disbursal {loan.disbursal_date.isoformat()} but Loan term is "
            f"{loan.loan_term_months} months",
            {
                "disbursal_date": loan.disbursal_date.isoformat(),
                "expected_repayment_date": loan.expected_repayment_date.isoformat(),
                "loan_term_months": loan.loan_term_months,
                "implied_months": months,
            },
        )


RULES = [
    B1ScheduleSumsToLoanAmount(),
    B2PrincipalSplitSumsToLoanAmount(),
    B3PaidPrincipalMatchesRepaid(),
    B4PaidInterestMatchesRepaid(),
    B5PendingInterestMatchesOutstanding(),
    B6OverdueInterestMatchesDelayInterest(),
    B7DaysLateMatchesDiary(),
    B11ExpectedRepaymentMatchesTerm(),
]
