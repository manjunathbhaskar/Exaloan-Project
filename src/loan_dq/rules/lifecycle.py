"""Category E - the loan status is a state machine; the diary must be in a compatible state."""

from __future__ import annotations

from loan_dq.ingest.model import PaymentType
from loan_dq.rules.base import (
    BaseRule,
    LoanContext,
    Outcome,
    Severity,
    due_dates,
    money,
    paid_portion,
    pending_portion,
    pending_total,
)


class E1RepaidLoanIsSettled(BaseRule):
    id = "E1"
    category = "lifecycle"
    severity = Severity.HIGH
    title = "Status 'repaid' => nothing outstanding, nothing pending, repayment date set"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        if not loan.is_repaid:
            return Outcome.skip("loan not in status 'repaid'")
        problems: list[str] = []
        evidence: dict[str, object] = {"loan_status": loan.loan_status}
        if (
            loan.outstanding_principal is not None
            and loan.outstanding_principal > ctx.config.tolerance.amount_eur
        ):
            problems.append(f"Outstanding principal is EUR {loan.outstanding_principal:,.2f}")
            evidence["outstanding_principal"] = money(loan.outstanding_principal)
        if loan.repayment_date is None and "Repayment date" not in loan.missing_optional_columns:
            problems.append("Repayment date is empty")
        if ctx.diary_ok:
            unpaid = [p for p in ctx.regular if pending_portion(p) > 0.0]
            if unpaid:
                amt = pending_total(unpaid)
                problems.append(
                    f"{len(unpaid)} payment component(s) across {due_dates(unpaid)} due date(s) "
                    f"with EUR {amt:,.2f} still unpaid in the diary"
                )
                evidence["unpaid_components"] = len(unpaid)
                evidence["unpaid_due_dates"] = due_dates(unpaid)
                evidence["unpaid_amount"] = money(amt)
                evidence["unpaid_states"] = sorted({p.state.value for p in unpaid})
        if not problems:
            return Outcome.ok()
        return Outcome.fail("loan is marked 'repaid' but " + "; ".join(problems), evidence)


class E2LiveLoanHasOpenSchedule(BaseRule):
    id = "E2"
    category = "lifecycle"
    severity = Severity.HIGH
    title = "Status 'granted' => open instalments remain and no repayment date"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        if not loan.is_live:
            return Outcome.skip("loan not in status 'granted'")
        problems: list[str] = []
        evidence: dict[str, object] = {"loan_status": loan.loan_status}
        if loan.repayment_date is not None:
            problems.append(f"Repayment date {loan.repayment_date.isoformat()} is set")
            evidence["repayment_date"] = loan.repayment_date.isoformat()
        if (
            loan.outstanding_principal is not None
            and abs(loan.outstanding_principal) <= ctx.config.tolerance.amount_eur
        ):
            problems.append("Outstanding principal is zero")
            evidence["outstanding_principal"] = money(loan.outstanding_principal)
        if ctx.diary_complete:
            unpaid = [p for p in ctx.regular if pending_portion(p) > 0.0]
            if not unpaid:
                problems.append("every payment component in the diary is already paid")
                evidence["unpaid_components"] = 0
        if not problems:
            return Outcome.ok()
        return Outcome.fail("loan is marked 'granted' (live) but " + "; ".join(problems), evidence)


class E3TerminatedLoanState(BaseRule):
    id = "E3"
    category = "lifecycle"
    severity = Severity.HIGH
    title = "Status 'terminated' => termination evidence in the diary"
    subsumed_by = ("F2",)

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        if not loan.is_terminated:
            return Outcome.skip("loan not in status 'terminated'")
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        termination_rows = [p for p in ctx.diary if p.type is PaymentType.TERMINATION]
        unpaid = [p for p in ctx.regular if pending_portion(p) > 0.0]
        if not termination_rows and not unpaid:
            return Outcome.fail(
                "loan is marked 'terminated' but the diary shows neither a termination repayment "
                "nor any unpaid payment component",
                {"loan_status": loan.loan_status},
            )
        pending_term = [p for p in termination_rows if pending_portion(p) > 0.0]
        # Still owed, not scheduled: part-paid rows contribute only their pending remainder.
        unpaid_amount = pending_total(unpaid)
        term_amount = pending_total(pending_term)
        unresolved = unpaid_amount + term_amount
        if unresolved <= ctx.config.tolerance.amount_eur:
            return Outcome.ok()
        n_due = due_dates(unpaid)
        return Outcome.fail(
            f"loan is terminated with EUR {unresolved:,.2f} unresolved: {len(unpaid)} unpaid "
            f"payment component(s) across {n_due} due date(s) totalling EUR {unpaid_amount:,.2f} "
            f"plus {len(pending_term)} pending termination claim(s) of EUR {term_amount:,.2f}",
            {
                "unresolved_amount": money(unresolved),
                "unpaid_components": len(unpaid),
                "unpaid_due_dates": n_due,
                "unpaid_amount": money(unpaid_amount),
                "pending_termination_rows": len(pending_term),
                "pending_termination_amount": money(term_amount),
            },
        )


class E4PaidRowsHaveDatesUnpaidDoNot(BaseRule):
    id = "E4"
    category = "lifecycle"
    severity = Severity.HIGH
    title = "Paid rows carry a repayment date; unpaid rows do not"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        paid_no_date = [p for p in ctx.diary if p.state.is_paid and p.actual_date is None]
        unpaid_with_date = [
            p
            for p in ctx.regular
            if not p.state.is_paid
            and paid_portion(p) <= 0.0
            and p.actual_date is not None
            and not p.is_settlement_marker
        ]
        if not paid_no_date and not unpaid_with_date:
            return Outcome.ok()
        parts = []
        if paid_no_date:
            p = paid_no_date[0]
            parts.append(
                f"{len(paid_no_date)} row(s) marked '{p.state.value}' with no repayment date "
                f"(e.g. {p.type.value} due {p.due_date.isoformat() if p.due_date else '?'}, "
                f"EUR {p.amount if p.amount is not None else '?'})"
            )
        if unpaid_with_date:
            parts.append(f"{len(unpaid_with_date)} unpaid row(s) that carry a repayment date")
        return Outcome.fail(
            "; ".join(parts),
            {"paid_without_date": len(paid_no_date), "unpaid_with_date": len(unpaid_with_date)},
        )


class E5NothingPendingAfterFullSettlement(BaseRule):
    id = "E5"
    category = "lifecycle"
    severity = Severity.HIGH
    title = "After a paid full early repayment no instalment is left pending"
    subsumed_by = ("E1",)

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        closings = [
            p
            for p in ctx.diary
            if p.type is PaymentType.FULL_EARLY
            and p.state.is_paid
            and p.actual_date is not None
            and not p.is_aborted_settlement
        ]
        if not closings:
            return Outcome.skip("no full early repayment")
        pending_after = [p for p in ctx.regular if pending_portion(p) > 0.0]
        if not pending_after:
            return Outcome.ok()
        closing = max(closings, key=lambda p: p.actual_date or ctx.as_of)
        assert closing.actual_date is not None
        owed = pending_total(pending_after)
        return Outcome.fail(
            f"loan fully repaid early on {closing.actual_date.isoformat()} but "
            f"{len(pending_after)} payment component(s) across {due_dates(pending_after)} due "
            f"date(s) (EUR {owed:,.2f}) remain unpaid",
            {
                "full_early_repayment_date": closing.actual_date.isoformat(),
                "pending_components": len(pending_after),
                "pending_due_dates": due_dates(pending_after),
                "pending_amount": money(owed),
            },
        )


RULES = [
    E1RepaidLoanIsSettled(),
    E2LiveLoanHasOpenSchedule(),
    E3TerminatedLoanState(),
    E4PaidRowsHaveDatesUnpaidDoNot(),
    E5NothingPendingAfterFullSettlement(),
]
