"""Category G - repayment-behaviour profile: frequency x severity x trend of lateness.

The profile is computed once per loan and attached to the report so a downstream scoring
model has deterministic features to consume. Only three rules produce findings, all mild:
habitual-but-minor lateness (info), a current delay outside the borrower's own pattern (low)
and a paid-delay drift away from a stable baseline (low). Frequency alone never produces a
high-severity finding - that is what protects the habitually-a-few-days-late clean loan.

G8 measures behaviour per *payment episode* - one distinct due date - so the principal,
interest and fee component rows of a single instalment count once, not three times. The
profile's ``instalments_*`` counters remain per component row (they feed G6/G7 and the report).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from statistics import median

from loan_dq.rules.base import Axis, BaseRule, LoanContext, Outcome, Severity


def paid_episodes(ctx: LoanContext) -> list[tuple[date, int]]:
    """Paid delay per distinct due date, in due-date order; an episode's delay is the longest
    delay among its component rows (the instalment is settled when its last component is)."""
    by_due: dict[date, int] = {}
    for p, d in ctx.paid_delays:
        if p.due_date is None:
            continue
        by_due[p.due_date] = max(by_due.get(p.due_date, d), d)
    return sorted(by_due.items())


@dataclass
class BehaviourProfile:
    instalments_paid: int
    instalments_unpaid: int
    paid_late_count: int
    paid_late_share: float | None
    median_delay_days: float | None
    worst_delay_days: int | None
    longest_late_streak: int
    trend_days: float | None
    usual_due_day: int | None
    usual_paid_day: int | None
    current_max_days_past_due: int | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def build_profile(ctx: LoanContext) -> BehaviourProfile | None:
    if not ctx.diary_ok:
        return None
    grace = ctx.config.lateness.grace_days
    ordered = sorted(ctx.paid_delays, key=lambda x: x[0].due_date or ctx.as_of)
    delays = [d for _, d in ordered]
    late_flags = [d > grace for d in delays]
    streak = best = 0
    for is_late in late_flags:
        streak = streak + 1 if is_late else 0
        best = max(best, streak)
    trend: float | None = None
    if len(delays) >= ctx.config.lateness.trend_min_paid:
        half = len(delays) // 2
        trend = round(sum(delays[half:]) / (len(delays) - half) - sum(delays[:half]) / half, 1)
    due_days = [p.due_date.day for p, _ in ordered if p.due_date]
    paid_days = [p.actual_date.day for p, _ in ordered if p.actual_date]
    dpd = [d for p in ctx.unpaid_rows if (d := ctx.days_past_due(p)) is not None and d > 0]
    return BehaviourProfile(
        instalments_paid=len(delays),
        instalments_unpaid=len(ctx.unpaid_rows),
        paid_late_count=sum(late_flags),
        paid_late_share=round(sum(late_flags) / len(delays), 3) if delays else None,
        median_delay_days=float(median(delays)) if delays else None,
        worst_delay_days=max(delays) if delays else None,
        longest_late_streak=best,
        trend_days=trend,
        usual_due_day=_mode(due_days),
        usual_paid_day=_mode(paid_days),
        current_max_days_past_due=max(dpd) if dpd else None,
    )


def _mode(values: list[int]) -> int | None:
    if not values:
        return None
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(sorted(counts), key=lambda k: counts[k])


class G6HabituallyLate(BaseRule):
    id = "G6"
    category = "behaviour"
    severity = Severity.INFO
    axis = Axis.CREDIT
    title = "Habitually late: frequency x severity x trend of delays"
    subsumed_by = ("F1", "F2", "F3")

    def evaluate(self, ctx: LoanContext) -> Outcome:
        profile = build_profile(ctx)
        if profile is None:
            return Outcome.skip("payment diary unusable")
        if profile.instalments_paid < ctx.config.lateness.trend_min_paid:
            return Outcome.skip("too few paid instalments to describe a habit")
        share = profile.paid_late_share or 0.0
        if share < ctx.config.lateness.habitual_late_share:
            return Outcome.ok()
        worst = profile.worst_delay_days or 0
        if worst >= ctx.config.lateness.default_days:
            verdict = "chronic lateness escalating to default"
        elif worst >= ctx.config.lateness.medium_days:
            verdict = "chronically late, occasionally beyond 30 days"
        else:
            verdict = "habitually a few days late, never seriously"
        direction = "worsening" if (profile.trend_days or 0) > 0 else "stable or improving"
        return Outcome.fail(
            f"{profile.paid_late_count} of {profile.instalments_paid} instalments paid more than "
            f"{ctx.config.lateness.grace_days} days late (median {profile.median_delay_days:g} d, "
            f"worst {worst} d, trend {profile.trend_days:+g} d, {direction}) - {verdict}",
            {
                "paid_late_share": share,
                "median_delay_days": profile.median_delay_days,
                "worst_delay_days": profile.worst_delay_days,
                "trend_days": profile.trend_days,
            },
        )


class G7OutsideOwnPattern(BaseRule):
    id = "G7"
    category = "behaviour"
    severity = Severity.LOW
    axis = Axis.CREDIT
    title = "Current delay exceeds the borrower's own historical worst"
    subsumed_by = ("F2", "F3")

    def evaluate(self, ctx: LoanContext) -> Outcome:
        profile = build_profile(ctx)
        if profile is None:
            return Outcome.skip("payment diary unusable")
        if not ctx.loan.is_live:
            return Outcome.skip("only meaningful for live loans")
        if profile.worst_delay_days is None or profile.current_max_days_past_due is None:
            return Outcome.ok()
        own_worst = max(profile.worst_delay_days, ctx.config.lateness.grace_days)
        if profile.current_max_days_past_due <= own_worst:
            return Outcome.ok()
        return Outcome.fail(
            f"an instalment is {profile.current_max_days_past_due} days past due, beyond this "
            f"borrower's historical worst of {profile.worst_delay_days} days",
            {
                "current_max_days_past_due": profile.current_max_days_past_due,
                "historical_worst_days": profile.worst_delay_days,
            },
        )


class G8DelayDriftFromBaseline(BaseRule):
    id = "G8"
    category = "behaviour"
    severity = Severity.LOW
    axis = Axis.CREDIT
    title = "Recent paid delays repeatedly exceed a previously stable baseline"
    subsumed_by = ("F1", "F2", "F3")

    def evaluate(self, ctx: LoanContext) -> Outcome:
        if not ctx.diary_ok:
            return Outcome.skip("payment diary unusable")
        cfg = ctx.config.lateness
        episodes = paid_episodes(ctx)
        window = cfg.drift_recent_episodes
        need = max(cfg.trend_min_paid, window + 1)
        if len(episodes) < need:
            return Outcome.skip(
                f"{len(episodes)} paid episode(s): need at least {need} to separate a baseline "
                f"from the last {window}"
            )
        baseline, recent = episodes[:-window], episodes[-window:]
        baseline_worst = max(d for _, d in baseline)
        if baseline_worst > cfg.grace_days:
            return Outcome.skip(
                f"baseline already irregular (worst {baseline_worst} d > grace {cfg.grace_days} d)"
            )
        exceeding = [(due, d) for due, d in recent if d > cfg.grace_days]
        if len(exceeding) < cfg.drift_min_exceeding:
            return Outcome.ok(
                {
                    "baseline_worst_days": baseline_worst,
                    "recent_delays_days": [d for _, d in recent],
                }
            )
        recent_txt = ", ".join(f"{d} d (due {due.isoformat()})" for due, d in exceeding)
        return Outcome.fail(
            f"stable payer drifting: {len(baseline)} earlier payment episodes were all settled "
            f"within {baseline_worst} day(s) of due, but {len(exceeding)} of the last {window} "
            f"episodes ran past the {cfg.grace_days}-day grace: {recent_txt}",
            {
                "baseline_episodes": len(baseline),
                "baseline_worst_days": baseline_worst,
                "recent_window": window,
                "recent_delays_days": [d for _, d in recent],
                "recent_exceeding": len(exceeding),
                "grace_days": cfg.grace_days,
            },
        )


RULES = [G6HabituallyLate(), G7OutsideOwnPattern(), G8DelayDriftFromBaseline()]
