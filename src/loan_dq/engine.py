"""Orchestration: read -> ingest -> evaluate every rule on every loan -> tape checks -> Report.

Guarantees:
* one unreadable row never stops the run (quarantined by ingestion);
* one crashing rule never stops a loan (caught here, reported as ``rule_error``);
* the same file + config + rule-set always yields the same report (no randomness, no clock
  in the verdict path - the as-of date is either configured or derived from the tape);
* opt-in plugins run last and may only annotate: verdicts are fingerprinted around them;
* a shard (``--shard i/n``) runs the identical per-loan path on a row range and defers the
  tape-level checks, tape health and plugins to ``merge``, which sees the whole tape.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from loan_dq.config import Config
from loan_dq.ingest.model import Loan
from loan_dq.ingest.schema import IngestResult, ingest
from loan_dq.io.reader import count_rows, read_tape
from loan_dq.plugins import apply_plugins
from loan_dq.report.health import health_partial, tape_health
from loan_dq.report.schema import (
    Finding,
    LoanResult,
    QuarantineRecord,
    Report,
    RuleOutcome,
    ShardInfo,
)
from loan_dq.rules.base import Axis, LoanContext, Outcome, Rule, Severity, Status, max_severity
from loan_dq.rules.behaviour import build_profile
from loan_dq.rules.registry import RULESET_VERSION, all_rules
from loan_dq.rules.tape import evaluate_tape, facts_of, flagged_population_profile

log = logging.getLogger("loan_dq")

DEFAULT_RULES = ("F1", "F2")
WATCH_RULES = ("F3", "C8", "G7")
RESIDUAL_KEYS = {
    "B1": "gap",
    "B2": "gap",
    "B3": "gap",
    "B4": "gap",
    "B5": "gap",
    "C2": "realised_xirr_pct",
    "F1": "worst_delay_days",
    "F2": "oldest_days_past_due",
}


@dataclass
class RuleRun:
    rule: Rule
    outcome: Outcome


def _run_rule(rule: Rule, ctx: LoanContext) -> Outcome:
    try:
        return rule.evaluate(ctx)
    except Exception as exc:  # a rule bug must degrade one check, not the loan or the run
        log.error("rule %s crashed on row %d (%s)", rule.id, ctx.loan.row_index, type(exc).__name__)
        return Outcome(
            Status.NOT_EVALUABLE,
            f"rule_error: {exc.__class__.__name__}",
            {"exception": exc.__class__.__name__},
        )


def evaluate_loan(loan: Loan, config: Config, as_of: date, rules: list[Rule]) -> LoanResult:
    ctx = LoanContext(loan, config, as_of)
    runs: list[RuleRun] = []
    for rule in rules:
        if rule.id in config.disabled_rules:
            continue
        runs.append(RuleRun(rule, _run_rule(rule, ctx)))

    failed_ids = {r.rule.id for r in runs if r.outcome.status is Status.FAIL}
    findings: list[Finding] = []
    outcomes: list[RuleOutcome] = []
    rule_errors = 0
    for r in runs:
        outcomes.append(RuleOutcome(r.rule.id, r.outcome.status.value, r.outcome.message))
        if r.outcome.status is Status.NOT_EVALUABLE and r.outcome.message.startswith("rule_error"):
            rule_errors += 1
        if r.outcome.status is not Status.FAIL:
            continue
        severity = r.outcome.severity or r.rule.severity
        if r.rule.id in config.info_only_rules:
            severity = Severity.INFO
        cause = (
            None
            if r.outcome.standalone
            else next((c for c in r.rule.subsumed_by if c in failed_ids), None)
        )
        findings.append(
            Finding(
                rule_id=r.rule.id,
                category=r.rule.category,
                axis=r.rule.axis.value,
                severity=severity.value,
                message=r.outcome.message,
                evidence=r.outcome.evidence,
                root_cause=cause is None,
                symptom_of=cause,
            )
        )

    reported = [f for f in findings if Severity(f.severity).counts_as_flag]
    # Symptoms are reported but the loan's severity is set by its root causes: a EUR 2 principal
    # shortfall (low) explaining a EUR 2 summary/diary gap (high) is one low-severity problem.
    roots = [f for f in reported if f.root_cause] or reported
    overall = max_severity([Severity(f.severity) for f in roots])
    flag_floor = Severity(config.verdict.flag_min_severity)
    is_flagged = overall is not None and overall.rank >= flag_floor.rank
    integrity_defect = any(
        f.axis == Axis.INTEGRITY.value and Severity(f.severity).rank >= flag_floor.rank
        for f in roots
    )
    if failed_ids & set(DEFAULT_RULES):
        credit = "default"
    elif failed_ids & set(WATCH_RULES):
        credit = "watch"
    else:
        credit = "none"
    missing_evidence = any(
        r.outcome.status is Status.NOT_EVALUABLE
        and r.rule.category in {"identity", "arithmetic", "temporal", "lifecycle", "lateness"}
        and any(
            word in r.outcome.message.lower() for word in ("missing", "column absent", "converge")
        )
        and not (r.rule.id == "D4" and not loan.is_repaid and loan.repayment_date is None)
        for r in runs
    )
    evaluated = any(r.outcome.status in (Status.PASS, Status.FAIL) for r in runs)
    if not loan.diary.readable:
        coverage = "none"
    elif (
        not loan.critical_evidence_complete
        or loan.loan_status not in config.schema_.loan_status_values
        or rule_errors
        or missing_evidence
        or not evaluated
    ):
        coverage = "partial"
    else:
        coverage = "full"
    integrity = "defect" if integrity_defect else "clean" if coverage == "full" else "unknown"

    residuals: dict[str, object] = {}
    for r in runs:
        key = RESIDUAL_KEYS.get(r.rule.id)
        if key and key in r.outcome.evidence:
            residuals[f"{r.rule.id}_{key}"] = r.outcome.evidence[key]
    profile = build_profile(ctx)

    assert loan.loan_id is not None
    return LoanResult(
        loan_id=loan.loan_id,
        row_index=loan.row_index,
        verdict="flagged" if is_flagged else "normal",
        severity=overall.value if overall else None,
        data_integrity=integrity,
        credit_event=credit,
        validation_coverage=coverage,
        findings=findings,
        rule_outcomes=outcomes,
        checks_passed=sum(1 for r in runs if r.outcome.status is Status.PASS),
        checks_failed=len(failed_ids),
        checks_not_evaluable=sum(1 for r in runs if r.outcome.status is Status.NOT_EVALUABLE),
        behaviour_profile=profile.as_dict() if profile else None,
        residuals=residuals,
        loan_status=loan.loan_status,
    )


class AsOfError(ValueError):
    pass


class TapeRejectedError(ValueError):
    pass


def resolve_as_of(config: Config, ingested: IngestResult) -> tuple[date, str]:
    if config.as_of is not None:
        return config.as_of, "config"
    if ingested.inferred_as_of is not None:
        source = "inferred:max_date_in_tape"
        gap = ingested.inferred_as_of_gap_days
        if gap is not None and gap > config.tolerance.as_of_isolated_days:
            source += (
                f" (isolated: next-latest date is {gap} days earlier - a future-dated row would "
                "be invisible to D2; pass --as-of)"
            )
        return ingested.inferred_as_of, source
    raise AsOfError(
        "cannot infer as-of: no usable observed dates in tape "
        f"({len(ingested.loans)} usable rows, {len(ingested.quarantined)} quarantined); "
        "supply an explicit --as-of date"
    )


@dataclass
class RunResult:
    """The report plus the parsed loans it was built from (for the payments tables)."""

    report: Report
    ingested: IngestResult


class ShardError(ValueError):
    """A shard request that cannot be honoured (bad spec, no rows, as-of not pinned)."""


def shard_bounds(rows: int, index: int, count: int) -> range:
    """Contiguous row range of shard ``index`` (1-based) out of ``count`` equal-as-possible cuts.
    The same (rows, count) always yields the same cut, so every shard can be re-run alone."""
    if count < 1 or not 1 <= index <= count:
        raise ShardError(f"shard index must be within 1..{count}, got {index}/{count}")
    if rows < 0:
        raise ShardError("row count must not be negative")
    base, extra = divmod(rows, count)
    start = (index - 1) * base + min(index - 1, extra)
    stop = start + base + (1 if index <= extra else 0)
    return range(start, stop)


def plan_shards(tape_path: Path, count: int) -> list[ShardInfo]:
    rows = count_rows(tape_path)
    if rows < 1 or count > rows:
        raise ShardError(f"shard count must be between 1 and the number of rows ({rows})")
    shard_bounds(rows, 1, count)
    return [
        ShardInfo(i, count, bounds.start, bounds.stop)
        for i in range(1, count + 1)
        for bounds in [shard_bounds(rows, i, count)]
    ]


def run(tape_path: Path, config: Config, *, rules: list[Rule] | None = None) -> Report:
    return run_pipeline(tape_path, config, rules=rules).report


def run_pipeline(
    tape_path: Path,
    config: Config,
    *,
    rules: list[Rule] | None = None,
    shard: tuple[int, int] | None = None,
) -> RunResult:
    started = time.perf_counter()
    rules = rules if rules is not None else all_rules()
    shard_info: ShardInfo | None = None
    if shard is None:
        frame = read_tape(tape_path)
        ingested = ingest(frame, config)
    else:
        if config.as_of is None:
            raise ShardError(
                "a shard cannot infer the as-of date from its own rows; pass --as-of "
                "(loan-dq plan prints the whole-tape inference)"
            )
        index, count = shard
        bounds = shard_bounds(count_rows(tape_path), index, count)
        if not bounds:
            raise ShardError(f"shard {index}/{count} covers no rows of {tape_path.name}")
        shard_info = ShardInfo(index, count, bounds.start, bounds.stop)
        frame = read_tape(tape_path, rows=bounds)
        ingested = ingest(frame, config, row_offset=bounds.start)
    if config.as_of is None and (not ingested.schema.ok or not ingested.loans):
        reason = (
            "; ".join(ingested.issues) or "required columns absent"
            if not ingested.schema.ok
            else f"no usable rows ({len(ingested.quarantined)} quarantined)"
        )
        raise TapeRejectedError(f"tape rejected: {reason}")
    as_of, as_of_source = resolve_as_of(config, ingested)
    log.info(
        "tape=%s rows=%d loans=%d quarantined=%d as_of=%s (%s)",
        tape_path.name,
        len(frame),
        len(ingested.loans),
        len(ingested.quarantined),
        as_of,
        as_of_source,
    )

    results: list[LoanResult] = []
    for loan in ingested.loans:
        result = evaluate_loan(loan, config, as_of, rules)
        results.append(result)
        if result.verdict != "normal":
            log.info(
                "loan=%s verdict=%s severity=%s rules=%s",
                loan.loan_id,
                result.verdict,
                result.severity,
                [f.rule_id for f in result.findings],
            )
    for q in ingested.quarantined:
        log.warning("row %d quarantined: %s", q.row_index, q.reason)

    whole_tape = shard_info is None and ingested.schema.ok
    tape_checks, tape_findings = (
        evaluate_tape(ingested.loans, results, config) if whole_tape else ([], [])
    )
    shard_facts: dict[str, object] | None = None
    if shard_info is not None and ingested.schema.ok:
        shard_facts = {
            "loans": [f.to_dict() for f in facts_of(ingested.loans, lender_scope=config.lender)],
            "health": health_partial(frame, ingested, config).to_dict(),
        }
    report = Report(
        input_file=tape_path.name,
        input_sha256=str(frame.attrs["input_sha256"]),
        rows_read=len(frame),
        as_of=as_of,
        as_of_source=as_of_source,
        # Plugins do not run in a shard (merge applies them), so they are not part of its digest.
        config_digest=(
            config if shard_info is None else config.model_copy(update={"plugins": []})
        ).digest(),
        ruleset_version=RULESET_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        schema_ok=ingested.schema.ok,
        required_columns_missing=ingested.schema.required_missing,
        optional_columns_missing=ingested.schema.optional_missing,
        loans=results,
        quarantined=[
            QuarantineRecord(q.row_index, q.loan_id_raw, q.reason) for q in ingested.quarantined
        ],
        tape_findings=tape_findings,
        circuit_breaker_tripped=any(t.rule_id == "I3" for t in tape_findings),
        duration_seconds=time.perf_counter() - started,
        tape_checks=tape_checks,
        flagged_profile=flagged_population_profile(results),
        tape_health=(
            tape_health(frame, ingested, results, as_of_source, config)
            if shard_info is None and ingested.schema.ok
            else None
        ),
        shard=shard_info,
        shard_facts=shard_facts,
    )
    if shard_info is None:
        report = apply_plugins(report, config.plugins)
    return RunResult(report=report, ingested=ingested)
