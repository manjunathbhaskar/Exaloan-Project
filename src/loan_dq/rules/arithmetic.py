"""Category C - does the money arithmetic hold? Includes the guarded XIRR-vs-stated-rate check."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from loan_dq.ingest.model import Payment, PaymentState, PaymentType
from loan_dq.rules.base import (
    Axis,
    BaseRule,
    LoanContext,
    Outcome,
    Severity,
    money,
    paid_portion,
    pending_portion,
    total,
)
from loan_dq.rules.xirr import CashFlow, xirr


class C1InstalmentInterestMatchesRate(BaseRule):
    id = "C1"
    category = "arithmetic"
    severity = Severity.MEDIUM
    title = "Each interest instalment = outstanding balance x rate / 12"
    subsumed_by = ("C2", "F1", "F2")

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=True)
        if skip:
            return skip
        loan = ctx.loan
        if loan.loan_amount is None or loan.interest_rate is None:
            return Outcome.skip("Loan amount or Interest rate missing")
        if loan.is_deferred_annuity:
            return Outcome.skip("deferred-annuity interest is not accrued per instalment")
        reductions: list[tuple[date, float]] = []
        for p in ctx.schedule_rows + ctx.settlement_cash:
            received = paid_portion(p)
            if received > 0.0:
                if p.actual_date is None:
                    return Outcome.skip(
                        "undated principal receipt: actual date missing for accrual reconstruction"
                    )
                reductions.append((p.actual_date, received))
            if p in ctx.schedule_rows and p.due_date is not None:
                reductions.append((p.due_date, pending_portion(p)))
        interest_rows = [
            p
            for p in ctx.interest_rows
            if p.type is PaymentType.INTEREST
            and not p.is_closure_row
            and p.due_date is not None
            and p.amount is not None
        ]
        if len(interest_rows) < 2:
            return Outcome.skip("fewer than two interest instalments")
        monthly_rate = loan.interest_rate / 100.0 / 12.0
        tol_rel = ctx.config.tolerance.instalment_interest_rel
        tol_eur = ctx.config.tolerance.instalment_interest_eur
        misses: list[dict[str, object]] = []
        for p in interest_rows:
            assert p.due_date is not None and p.amount is not None
            balance = loan.loan_amount - sum(a for d, a in reductions if d < p.due_date)
            expected = max(balance, 0.0) * monthly_rate
            if abs(p.amount - expected) > max(tol_eur, tol_rel * expected):
                misses.append(
                    {
                        "due_date": p.due_date.isoformat(),
                        "interest_row": money(p.amount),
                        "expected": money(expected),
                        "balance": money(balance),
                    }
                )
        share = len(misses) / len(interest_rows)
        if share <= 0.5 or len(misses) < 2:
            isolated = self._isolated_material(ctx, interest_rows, reductions, misses)
            if isolated:
                first = isolated[0]
                return Outcome.fail(
                    f"isolated material interest excess: due {first['due_date']}, row EUR "
                    f"{first['interest_row']} exceeds both the accrual upper bound EUR "
                    f"{first['accrual_upper_bound']} and the other instalments; "
                    "neighbouring accruals support the stated-rate schedule; review the amount",
                    {
                        "stated_rate_pct": loan.interest_rate,
                        "instalments_checked": len(interest_rows),
                        "instalments_off": len(isolated),
                        "examples": isolated[:3],
                        "independent_amount_evidence": True,
                    },
                    standalone=True,
                )
            return Outcome.ok()
        first = misses[0]
        return Outcome.fail(
            f"{len(misses)} of {len(interest_rows)} interest instalments do not match "
            f"balance x {loan.interest_rate:g}% / 12 (e.g. due {first['due_date']}: row EUR "
            f"{first['interest_row']}, expected EUR {first['expected']})",
            {
                "stated_rate_pct": loan.interest_rate,
                "instalments_checked": len(interest_rows),
                "instalments_off": len(misses),
                "examples": misses[:3],
            },
        )

    @staticmethod
    def _isolated_material(
        ctx: LoanContext,
        rows: list[Payment],
        reductions: list[tuple[date, float]],
        misses: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        loan = ctx.loan
        if loan.disbursal_date is None or loan.loan_amount is None or not loan.interest_rate:
            return []
        ordered = sorted(rows, key=lambda p: p.due_date or ctx.as_of)
        if len(ordered) < 3 or len({p.due_date for p in ordered}) != len(ordered):
            return []
        bad_dates = {m["due_date"] for m in misses}
        tol = ctx.config.tolerance
        isolated: list[dict[str, object]] = []
        for i, p in enumerate(ordered):
            assert p.due_date is not None and p.amount is not None
            if p.due_date.isoformat() not in bad_dates:
                continue
            peers = sorted((j for j in range(len(ordered)) if j != i), key=lambda j: abs(j - i))[:2]
            if any(
                (due := ordered[j].due_date) is None or due.isoformat() in bad_dates for j in peers
            ):
                continue
            start = ordered[i - 1].due_date if i else loan.disbursal_date
            assert start is not None
            days = (p.due_date - start).days
            if days <= 0 or (i and not tol.schedule_min_gap_days <= days <= tol.schedule_gap_days):
                continue
            balance = max(loan.loan_amount - sum(a for d, a in reductions if d < p.due_date), 0.0)
            accrual_bound = balance * loan.interest_rate / 100.0 * max(days / 360.0, 1.0 / 12.0)
            peer_max = max(q.amount or 0.0 for q in ordered if q is not p)
            upper = max(accrual_bound, peer_max)
            if p.amount - upper <= max(tol.interest_eur, tol.instalment_interest_rel * upper):
                continue
            isolated.append(
                {
                    "due_date": p.due_date.isoformat(),
                    "interest_row": money(p.amount),
                    "accrual_upper_bound": money(accrual_bound),
                    "other_instalments_max": money(peer_max),
                    "accrual_days": days,
                    "day_count_envelope": "max(monthly, actual/360)",
                    "supporting_row_indexes": [ordered[j].index for j in peers],
                    "row_index": p.index,
                }
            )
        return isolated


class C2RealisedRateMatchesStated(BaseRule):
    id = "C2"
    category = "arithmetic"
    severity = Severity.HIGH
    title = "XIRR of actual cash flows ~ stated interest rate"
    # A realised rate dragged below the stated one by 90+ day late payments is the credit event
    # itself, not a second defect - F1/F2 own that finding.
    subsumed_by = ("B1", "F1", "F2")

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        loan = ctx.loan
        cfg = ctx.config.xirr
        if loan.loan_amount is None or loan.interest_rate is None or loan.disbursal_date is None:
            return Outcome.skip("Loan amount / Interest rate / Disbursal date missing")

        cash_rows = [p for p in ctx.diary if paid_portion(p) > 0.0]
        if any(p.actual_date is None for p in cash_rows):
            return Outcome.skip(
                "undated receipt(s): actual dates missing; XIRR timing cannot be established"
            )
        if any(
            p.actual_date is not None
            and (p.actual_date < loan.disbursal_date or p.actual_date > ctx.as_of)
            for p in cash_rows
        ):
            return Outcome.skip(
                "receipt dates outside disbursal/as-of bounds; review cash-flow timing"
            )
        inflows: list[CashFlow] = [
            (p.actual_date, paid_portion(p)) for p in cash_rows if p.actual_date is not None
        ]
        if len(inflows) < cfg.min_inflows:
            return Outcome.skip(f"fewer than {cfg.min_inflows} paid cash flows")

        flows: list[CashFlow] = [(loan.disbursal_date, -loan.loan_amount), *inflows]
        terminal_note = ""
        if loan.is_live:
            if loan.is_deferred_annuity:
                return Outcome.skip("live deferred annuity: interest realised only at maturity")
            if loan.diary.truncated:
                return Outcome.skip("live loan with truncated diary: paid flows incomplete")
            all_interest = total(
                [
                    p
                    for p in ctx.interest_rows
                    if p.type is PaymentType.INTEREST and not p.is_closure_row
                ]
            )
            paid_interest = sum(
                paid_portion(p)
                for p in ctx.interest_rows
                if p.type is PaymentType.INTEREST and not p.is_closure_row
            )
            if all_interest <= 0 or paid_interest / all_interest < cfg.min_realised_interest_share:
                return Outcome.skip("too little interest realised yet to measure a rate")
            if loan.outstanding_principal is None:
                return Outcome.skip("live loan without Outstanding principal for terminal value")
            flows.append((ctx.as_of, loan.outstanding_principal))
            terminal_note = f" (outstanding EUR {loan.outstanding_principal:,.2f} as terminal flow)"
        elif loan.diary.truncated:
            return Outcome.skip("closed loan with truncated diary: paid flows incomplete")

        rate = xirr(flows, low=cfg.low_rate_floor, high=cfg.high_rate_ceiling)
        if rate is None:
            return Outcome.skip("XIRR did not converge for these cash flows")
        realised_pct = rate * 100.0
        gap = realised_pct - loan.interest_rate
        threshold = max(cfg.gap_percentage_points, cfg.gap_relative * abs(loan.interest_rate))
        if abs(gap) <= threshold:
            return Outcome.ok({"realised_xirr_pct": round(realised_pct, 2)})
        paid_interest_total = sum(
            paid_portion(p) for p in ctx.interest_rows if not p.is_closure_row
        )
        zero_interest = paid_interest_total <= ctx.config.tolerance.interest_eur
        confidence, why = self._confidence(ctx)
        principal_gap = total(ctx.schedule_rows) + total(ctx.settlement_cash) - loan.loan_amount
        principal_shortfall = gap < 0 and principal_gap < -ctx.config.tolerance.amount_eur
        if principal_shortfall:
            confidence = "low"
            why += (
                "; diary principal shortfall makes the negative XIRR an uncertain rate comparison"
            )
        on_schedule: list[CashFlow] = [(loan.disbursal_date, -loan.loan_amount)]
        for p in cash_rows:
            assert p.actual_date is not None
            d = p.actual_date
            if p.is_regular and p.due_date is not None and d > p.due_date:
                d = p.due_date
            on_schedule.append((d, paid_portion(p)))
        if loan.is_live and loan.outstanding_principal is not None:
            on_schedule.append((ctx.as_of, loan.outstanding_principal))
        scheduled_rate = xirr(on_schedule, low=cfg.low_rate_floor, high=cfg.high_rate_ceiling)
        scheduled_pct = scheduled_rate * 100.0 if scheduled_rate is not None else None
        timing_explains_gap = (
            gap < 0
            and scheduled_pct is not None
            and abs(scheduled_pct - loan.interest_rate) <= threshold
        )
        independent_rate_evidence = (
            not principal_shortfall
            and confidence != "low"
            and (
                gap > 0 or zero_interest or (scheduled_pct is not None and not timing_explains_gap)
            )
        )
        msg = (
            f"stated rate {loan.interest_rate:g}% versus realised {realised_pct:.1f}% XIRR"
            f"{terminal_note}"
        )
        if confidence == "low":
            msg += " - review only, not a confirmed rate defect"
        else:
            msg += " - inconsistent with the stated " + (
                "product" if loan.is_deferred_annuity else "rate"
            )
        msg += f" (diary shows EUR {paid_interest_total:,.2f} interest actually paid)"
        if loan.is_deferred_annuity and confidence != "high":
            msg += "; review against early-settlement/deferred-annuity terms"
        if principal_shortfall:
            msg += f"; principal shortfall EUR {abs(principal_gap):,.2f} is linked to B1"
        if timing_explains_gap:
            msg += "; on-schedule receipt timing brings the rate within tolerance"
        severity = Severity.HIGH if abs(gap) > 2 * threshold or zero_interest else Severity.MEDIUM
        if confidence == "low":
            severity = Severity.LOW
        return Outcome.fail(
            msg,
            {
                "stated_rate_pct": loan.interest_rate,
                "realised_xirr_pct": round(realised_pct, 2),
                "gap_pct_points": round(gap, 2),
                "threshold_pct_points": round(threshold, 2),
                "cash_flows": len(flows),
                "paid_interest_total": money(paid_interest_total),
                "confidence": confidence,
                "confidence_reason": why,
                "principal_gap": money(principal_gap),
                "linked_principal_rule": "B1" if principal_shortfall else None,
                "on_schedule_xirr_pct": money(scheduled_pct),
                "timing_explains_gap": timing_explains_gap,
                "independent_rate_evidence": independent_rate_evidence,
            },
            severity=severity,
            standalone=independent_rate_evidence,
        )

    @staticmethod
    def _confidence(ctx: LoanContext) -> tuple[str, str]:
        """How far the realised rate can be trusted to measure the stated one. Derived from the
        loan's own product and settlement facts, never from its identity: a deferred annuity
        settled before any scheduled interest fell due realised no interest by construction, so
        the mismatch may be contract behaviour rather than a defect."""
        loan = ctx.loan
        if not loan.is_deferred_annuity:
            return "high", "instalment product: interest accrues per instalment"
        settled = loan.repayment_date or max(
            (p.actual_date for p in ctx.diary if p.state.is_paid and p.actual_date), default=None
        )
        if settled is None:
            return "medium", "deferred annuity with no settlement date recorded"
        interest_fell_due = any(
            p.type is PaymentType.INTEREST
            and not p.is_closure_row
            and p.due_date is not None
            and p.due_date < settled
            for p in ctx.interest_rows
        )
        early = loan.expected_repayment_date is None or settled < loan.expected_repayment_date
        if early and not interest_fell_due:
            return (
                "low",
                f"deferred annuity settled early on {settled.isoformat()}, before any scheduled "
                "interest instalment fell due: whether early settlement waives the deferred "
                "interest is a contract term the tape does not carry",
            )
        if early:
            return "medium", "deferred annuity settled before its expected repayment date"
        return "high", "deferred annuity that ran to its interest schedule"


class C4NoNegativeAmounts(BaseRule):
    id = "C4"
    category = "arithmetic"
    severity = Severity.CRITICAL
    title = "No negative amounts anywhere"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        loan = ctx.loan
        negatives: dict[str, float] = {}
        for name, value in (
            ("Loan amount", loan.loan_amount),
            ("Outstanding principal", loan.outstanding_principal),
            ("Repaid principal", loan.repaid_principal),
            ("Outstanding interest", loan.outstanding_interest),
            ("Repaid interest", loan.repaid_interest),
            ("Monthly payment", loan.monthly_payment),
            ("Arrears", loan.arrears),
            ("Delay interest", loan.delay_interest),
            ("Days late", None if loan.days_late is None else float(loan.days_late)),
        ):
            if value is not None and value < 0:
                negatives[name] = round(value, 2)
        neg_rows = [
            p
            for p in ctx.diary
            if (p.amount is not None and p.amount < 0)
            or (p.pending_amount is not None and p.pending_amount < 0)
        ]
        if not negatives and not neg_rows:
            return Outcome.ok()
        parts = [f"{k} = {v:,.2f}" for k, v in negatives.items()]
        if neg_rows:
            parts.append(f"{len(neg_rows)} diary row(s) with negative amount")
        return Outcome.fail(
            "unexpected negative value(s): " + "; ".join(parts),
            {"summary_fields": negatives, "negative_diary_rows": len(neg_rows)},
        )


class C5PendingNotAboveAmount(BaseRule):
    id = "C5"
    category = "arithmetic"
    severity = Severity.HIGH
    title = "Pending amount never exceeds row amount"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        bad = [
            p
            for p in ctx.diary
            if p.amount is not None
            and p.pending_amount is not None
            and p.pending_amount > p.amount + 0.005
        ]
        if not bad:
            return Outcome.ok()
        p = bad[0]
        return Outcome.fail(
            f"{len(bad)} diary row(s) have pending amount above the row amount "
            f"(e.g. pending EUR {p.pending_amount:,.2f} on a EUR {p.amount:,.2f} row)",
            {"rows": len(bad), "example_row_index": p.index},
        )


class C6StateAgreesWithPendingAmount(BaseRule):
    id = "C6"
    category = "arithmetic"
    severity = Severity.HIGH
    title = "Paid rows have zero pending; unpaid rows have full pending"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        paid_with_pending: list[Payment] = []
        pending_above_amount: list[Payment] = []
        part_paid: list[Payment] = []
        for p in ctx.diary:
            if p.amount is None or p.pending_amount is None or p.is_settlement_marker:
                continue
            if p.state.is_paid and p.pending_amount > 0.005:
                paid_with_pending.append(p)
            elif p.state in (PaymentState.PENDING, PaymentState.PENDING_LATE, PaymentState.GRACE):
                if p.pending_amount > p.amount + 0.005:
                    pending_above_amount.append(p)
                elif p.pending_amount < p.amount - 0.005:
                    part_paid.append(p)
        if not paid_with_pending and not pending_above_amount and not part_paid:
            return Outcome.ok()
        parts = []
        if paid_with_pending:
            parts.append(f"{len(paid_with_pending)} paid row(s) still show a pending amount")
        if pending_above_amount:
            p = pending_above_amount[0]
            parts.append(
                f"{len(pending_above_amount)} unpaid row(s) with pending amount above the row "
                f"amount (e.g. amount EUR {p.amount:,.2f}, pending EUR {p.pending_amount:,.2f})"
            )
        contradiction = bool(paid_with_pending or pending_above_amount)
        if part_paid:
            p = part_paid[0]
            undated = sum(q.actual_date is None for q in part_paid)
            parts.append(
                f"{len(part_paid)} open row(s) partially paid; {undated} with no repayment date "
                f"recorded for the part-payment (e.g. state {p.state.value!r}, "
                f"amount EUR {p.amount:,.2f}, pending EUR {p.pending_amount:,.2f})"
                if undated
                else f"{len(part_paid)} open row(s) partially paid with dated receipt evidence "
                f"(e.g. state {p.state.value!r}, amount EUR {p.amount:,.2f}, "
                f"pending EUR {p.pending_amount:,.2f})"
            )
        return Outcome.fail(
            "; ".join(parts),
            {
                "paid_with_pending": len(paid_with_pending),
                "pending_above_amount": len(pending_above_amount),
                "part_paid_open_rows": len(part_paid),
            },
            severity=Severity.HIGH if contradiction else Severity.LOW,
        )


class C8PaymentsStopped(BaseRule):
    id = "C8"
    category = "arithmetic"
    severity = Severity.MEDIUM
    axis = Axis.CREDIT
    title = "Remaining principal keeps falling (payments have not stopped)"
    subsumed_by = ("F2",)

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        if not ctx.loan.is_live:
            return Outcome.skip("only meaningful for live loans")
        due_rows = sorted(
            (
                p
                for p in ctx.regular
                if p.type is PaymentType.PRINCIPAL and p.due_date and p.due_date <= ctx.as_of
            ),
            key=lambda p: p.due_date or ctx.as_of,
        )
        streak = 0
        for p in reversed(due_rows):
            if p.state.is_paid:
                break
            streak += 1
        if streak < 3:
            return Outcome.ok()
        oldest = due_rows[-streak]
        assert oldest.due_date is not None
        return Outcome.fail(
            f"no principal paid for the last {streak} scheduled instalments "
            f"(since {oldest.due_date.isoformat()}); remaining balance is flat",
            {"unpaid_consecutive": streak, "since": oldest.due_date.isoformat()},
        )


class C10ConflictingRowsSameSlot(BaseRule):
    id = "C10"
    category = "arithmetic"
    severity = Severity.MEDIUM
    title = "One schedule row per (payment type, due date); no near-duplicates"
    # Overdue-interest rows are accrual items and legitimately repeat per due date; closure
    # rows and byte-identical duplicates are tagged at ingestion and owned by C9.
    _SLOT_TYPES = (PaymentType.PRINCIPAL, PaymentType.INTEREST, PaymentType.FEE)

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        slots: dict[tuple[PaymentType, date], list[Payment]] = defaultdict(list)
        for p in ctx.regular:
            if p.type in self._SLOT_TYPES and p.due_date is not None:
                slots[(p.type, p.due_date)].append(p)
        conflicts = [(k, rows) for k, rows in sorted(slots.items()) if len(rows) > 1]
        if not conflicts:
            return Outcome.ok()
        groups: list[dict[str, object]] = []
        any_unpaid = any(not p.state.is_paid for _, rows in conflicts for p in rows)
        for (ptype, due), rows in conflicts:
            groups.append(
                {
                    "type": ptype.value,
                    "due_date": due.isoformat(),
                    "rows": len(rows),
                    "differs_in": self._differing_fields(rows),
                    "amounts": [money(p.amount) for p in rows],
                    "states": [p.state.value for p in rows],
                    "row_indexes": [p.index for p in rows],
                }
            )
        (ptype, due), rows = conflicts[0]
        amounts = " / ".join("EUR ?" if p.amount is None else f"EUR {p.amount:,.2f}" for p in rows)
        return Outcome.fail(
            f"{len(groups)} due-date slot(s) hold more than one row of the same payment type with "
            f"different values (e.g. {ptype.value} due {due.isoformat()}: {len(rows)} rows, "
            f"{amounts}, differing in {', '.join(self._differing_fields(rows))})",
            {"slots": groups[:5], "slots_total": len(groups)},
            # Two paid rows for one slot is a ledger-shape defect with no open exposure; an
            # unpaid twin means the tape disagrees with itself about what is still owed.
            severity=Severity.MEDIUM if any_unpaid else Severity.LOW,
        )

    @staticmethod
    def _differing_fields(rows: list[Payment]) -> list[str]:
        out: list[str] = []
        if len({p.amount for p in rows}) > 1:
            out.append("amount")
        if len({p.pending_amount for p in rows}) > 1:
            out.append("pending_amount")
        if len({p.state for p in rows}) > 1:
            out.append("state")
        if len({p.actual_date for p in rows}) > 1:
            out.append("actual_date")
        return out


class C9NoZeroOrDuplicateRows(BaseRule):
    id = "C9"
    category = "arithmetic"
    severity = Severity.MEDIUM
    title = "No zero-amount paid rows and no duplicated schedule rows"

    def evaluate(self, ctx: LoanContext) -> Outcome:
        skip = ctx.skip_if_diary_unusable(need_complete=False)
        if skip:
            return skip
        zero_paid = [
            p
            for p in ctx.diary
            if p.state.is_paid
            and p.amount is not None
            and abs(p.amount) < 0.005
            and p.type not in (PaymentType.FULL_EARLY, PaymentType.PARTIAL_EARLY)
        ]
        dups = [p for p in ctx.diary if p.is_duplicate]
        if not zero_paid and not dups:
            return Outcome.ok()
        parts = []
        if zero_paid:
            parts.append(f"{len(zero_paid)} paid row(s) with EUR 0.00 amount")
        if dups:
            parts.append(f"{len(dups)} byte-identical duplicate schedule/interest row(s)")
        return Outcome.fail(
            "; ".join(parts), {"zero_amount_paid_rows": len(zero_paid), "duplicate_rows": len(dups)}
        )


RULES = [
    C1InstalmentInterestMatchesRate(),
    C2RealisedRateMatchesStated(),
    C4NoNegativeAmounts(),
    C5PendingNotAboveAmount(),
    C6StateAgreesWithPendingAmount(),
    C8PaymentsStopped(),
    C9NoZeroOrDuplicateRows(),
    C10ConflictingRowsSameSlot(),
]
