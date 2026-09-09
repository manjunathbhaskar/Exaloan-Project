"""Merge shard reports into the report a single run over the whole tape would have produced.

A shard report (``loan-dq run tape --shard i/n --as-of D``) already holds final per-loan
verdicts - the per-loan path is identical to a full run and reads nothing outside its loan.
What a shard cannot know is the rest of the tape, so it carries ``shard_facts``: the few
per-loan values the Category I checks need and the additive ``HealthPartial`` counters. Merge
concatenates the verdicts, evaluates the tape-level checks and tape health once over the
combined facts, then runs plugins (which rank across the whole tape) last.

Merge refuses to combine shards that disagree on the input's SHA-256, as-of date, config or
rule-set, or that do not cover the tape exactly once.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import date, datetime, timezone
from itertools import pairwise
from pathlib import Path

from loan_dq.config import Config
from loan_dq.plugins import apply_plugins
from loan_dq.report.health import HealthPartial, combine, finish
from loan_dq.report.schema import (
    Finding,
    LoanResult,
    QuarantineRecord,
    Report,
    RuleOutcome,
    ShardInfo,
)
from loan_dq.rules.base import check_group
from loan_dq.rules.registry import RULESET_VERSION
from loan_dq.rules.tape import LoanFacts, evaluate_tape_facts, flagged_population_profile


class MergeError(ValueError):
    """Shard reports that cannot be merged into one consistent tape report."""


def load_report_dict(path: Path) -> dict[str, object]:
    try:
        with path.open(encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError, UnicodeError):
        raise MergeError("shard report could not be read as JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("meta"), dict):
        raise MergeError("not a loan-dq report")
    return doc


def loan_result_from_dict(d: dict[str, object]) -> LoanResult:
    if type(d["loan_id"]) is not int:
        raise MergeError("invalid parsed loan identifier")
    for key in ("row_index", "checks_passed", "checks_failed", "checks_not_evaluable"):
        _integer(d[key])
    if _integer(d["source_row_index"]) != d["row_index"]:
        raise MergeError("loan source row identity mismatch")
    choices: dict[str, set[object]] = {
        "verdict": {"normal", "flagged", "unreadable"},
        "severity": {None, "info", "low", "medium", "high", "critical"},
        "data_integrity": {"clean", "defect", "unknown"},
        "credit_event": {"none", "watch", "default", "unknown"},
        "validation_coverage": {"full", "partial", "none"},
    }
    for key, allowed in choices.items():
        if d[key] not in allowed:
            raise MergeError("invalid loan result status")
    if d.get("loan_status") is not None and not isinstance(d["loan_status"], str):
        raise MergeError("invalid loan status type")
    findings_raw = _as_list(d.get("findings"))
    outcomes_raw = _as_list(d.get("rule_outcomes"))
    for item in findings_raw:
        finding = _as_dict(item)
        for key in ("rule_id", "category", "axis", "severity", "message"):
            if not isinstance(finding[key], str):
                raise MergeError("invalid finding field type")
        if type(finding["root_cause"]) is not bool:
            raise MergeError("invalid root cause flag")
        if "group" in finding and finding["group"] != check_group(str(finding["rule_id"])):
            raise MergeError("finding check group does not match rule identifier")
    for item in outcomes_raw:
        outcome = _as_dict(item)
        if any(not isinstance(outcome[key], str) for key in ("rule_id", "status", "note")):
            raise MergeError("invalid rule outcome type")
        if "group" in outcome and outcome["group"] != check_group(str(outcome["rule_id"])):
            raise MergeError("outcome check group does not match rule identifier")
    profile = None if d.get("behaviour_profile") is None else _as_dict(d["behaviour_profile"])
    residuals = _as_dict(d.get("residuals") or {})
    annotations = _as_dict(d.get("annotations") or {})
    result = LoanResult(
        loan_id=int(str(d["loan_id"])),
        row_index=int(str(d["row_index"])),
        verdict=str(d["verdict"]),
        severity=None if d.get("severity") is None else str(d["severity"]),
        data_integrity=str(d["data_integrity"]),
        credit_event=str(d["credit_event"]),
        validation_coverage=str(d["validation_coverage"]),
        findings=[
            Finding(
                rule_id=str(f["rule_id"]),
                category=str(f["category"]),
                axis=str(f["axis"]),
                severity=str(f["severity"]),
                message=str(f["message"]),
                evidence=_as_dict(f.get("evidence") or {}),
                root_cause=bool(f.get("root_cause", True)),
                symptom_of=None if f.get("symptom_of") is None else str(f["symptom_of"]),
            )
            for f in map(_as_dict, findings_raw)
        ],
        rule_outcomes=[
            RuleOutcome(str(o["rule_id"]), str(o["status"]), str(o.get("note", "")))
            for o in map(_as_dict, outcomes_raw)
        ],
        checks_passed=int(str(d["checks_passed"])),
        checks_failed=int(str(d["checks_failed"])),
        checks_not_evaluable=int(str(d["checks_not_evaluable"])),
        behaviour_profile=profile,
        residuals=residuals,
        loan_status=None if d.get("loan_status") is None else str(d["loan_status"]),
        annotations=annotations,
    )
    if "finding_groups" in d and d["finding_groups"] != result.finding_groups:
        raise MergeError("loan finding groups do not match its findings")
    counts = Counter(o.status for o in result.rule_outcomes)
    if (
        set(counts) - {"pass", "fail", "not_evaluable"}
        or result.checks_passed != counts["pass"]
        or result.checks_failed != counts["fail"]
        or result.checks_not_evaluable != counts["not_evaluable"]
        or len({o.rule_id for o in result.rule_outcomes}) != len(result.rule_outcomes)
        or Counter(f.rule_id for f in result.findings)
        != Counter(o.rule_id for o in result.rule_outcomes if o.status == "fail")
        or any(f.severity not in choices["severity"] - {None} for f in result.findings)
        or result.annotations
    ):
        raise MergeError("loan result checks or annotations are inconsistent")
    return result


def _meta(doc: dict[str, object]) -> dict[str, object]:
    return _as_dict(doc["meta"])


def _check_consistent(docs: list[dict[str, object]]) -> list[ShardInfo]:
    if not docs:
        raise MergeError("no shard reports given")
    shards: list[ShardInfo] = []
    for doc in docs:
        meta = _meta(doc)
        raw = meta.get("shard")
        if not isinstance(raw, dict):
            raise MergeError(f"{meta.get('input_file')}: not a shard report (run with --shard i/n)")
        for key in ("index", "count", "row_start", "row_stop"):
            _integer(raw[key])
        shard = ShardInfo.from_dict(raw)
        if shard.count < 1 or shard.row_stop <= shard.row_start:
            raise MergeError("invalid shard bounds")
        if _integer(meta["rows_read"]) != shard.row_stop - shard.row_start:
            raise MergeError("shard row count does not match its range")
        for key in ("required_columns_missing", "optional_columns_missing"):
            if any(not isinstance(c, str) for c in _as_list(meta[key])):
                raise MergeError("invalid schema column names")
        for key in (
            "input_file",
            "input_sha256",
            "as_of",
            "as_of_source",
            "config_digest",
            "ruleset_version",
        ):
            if not isinstance(meta[key], str) or not meta[key]:
                raise MergeError("invalid report metadata")
        if re.fullmatch(r"[a-f0-9]{64}", str(meta["input_sha256"])) is None:
            raise MergeError("invalid input_sha256")
        shards.append(shard)
    head = _meta(docs[0])
    for key in (
        "input_sha256",
        "as_of",
        "as_of_source",
        "config_digest",
        "ruleset_version",
        "schema_ok",
        "required_columns_missing",
        "optional_columns_missing",
    ):
        values = {json.dumps(_meta(d).get(key), default=str) for d in docs}
        if len(values) > 1:
            raise MergeError(f"shard reports disagree on {key}")
    if head.get("schema_ok") is not True:
        raise MergeError(
            "shards failed the schema check (required columns missing: "
            f"{head.get('required_columns_missing')}); nothing to merge"
        )
    if head["ruleset_version"] != RULESET_VERSION:
        raise MergeError("unsupported ruleset_version; rerun shards with the current ruleset")
    if head["required_columns_missing"]:
        raise MergeError("schema_ok contradicts missing required columns")
    count = shards[0].count
    if any(s.count != count for s in shards):
        raise MergeError(f"shard reports come from different cuts: {[s.count for s in shards]}")
    indexes = sorted(s.index for s in shards)
    if indexes != list(range(1, count + 1)):
        missing = sorted(set(range(1, count + 1)) - set(indexes))
        raise MergeError(
            f"expected shards 1..{count} exactly once, got {indexes}"
            + (f"; missing {missing}" if missing else "")
        )
    ordered = sorted(shards, key=lambda s: s.index)
    total_rows = sum(_integer(_meta(d)["rows_read"]) for d in docs)
    if ordered[0].row_start != 0 or ordered[-1].row_stop != total_rows:
        raise MergeError("shard boundaries must start at 0 and end at total rows")
    for prev, nxt in pairwise(ordered):
        if prev.row_stop != nxt.row_start:
            raise MergeError(f"shards {prev.label} and {nxt.label} do not tile the tape")
    return shards


def merge_reports(docs: list[dict[str, object]], config: Config) -> Report:
    try:
        return _merge_reports(docs, config)
    except MergeError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, AssertionError, OverflowError):
        raise MergeError("malformed shard report schema or counters") from None


def _merge_reports(docs: list[dict[str, object]], config: Config) -> Report:
    started = time.perf_counter()
    shards = _check_consistent(docs)
    head = _meta(docs[0])
    as_of = date.fromisoformat(str(head["as_of"]))
    # The shards were pinned to one as-of date; the merge inherits it. Plugins run here, not in
    # the shards, so they are compared without them (see the shard digest in engine.run_pipeline).
    config = config.model_copy(update={"as_of": as_of})
    deterministic_digest = config.model_copy(update={"plugins": []}).digest()
    if deterministic_digest != head["config_digest"]:
        raise MergeError(
            "merge config differs from the shards' config "
            f"({deterministic_digest} vs {head['config_digest']}); use the same --config"
        )
    order = sorted(range(len(docs)), key=lambda i: shards[i].index)

    results: list[LoanResult] = []
    quarantined: list[QuarantineRecord] = []
    facts: list[LoanFacts] = []
    partials: list[HealthPartial] = []
    for i in order:
        doc = docs[i]
        sf = doc.get("shard_facts")
        if not isinstance(sf, dict):
            raise MergeError(f"shard {shards[i].label} carries no shard_facts")
        if set(sf) != {"loans", "health"}:
            raise MergeError("unsupported shard_facts schema")
        shard_results = [loan_result_from_dict(_as_dict(r)) for r in _as_list(doc.get("loans"))]
        rows = range(shards[i].row_start, shards[i].row_stop)
        shard_quarantined = []
        for q in map(_as_dict, _as_list(doc.get("quarantined"))):
            row_index = _integer(q["row_index"])
            if _integer(q["source_row_index"]) != row_index:
                raise MergeError("quarantine source row identity mismatch")
            shard_quarantined.append(
                QuarantineRecord(row_index, str(q["loan_id_raw"]), str(q["reason"]))
            )
        row_indexes = [r.row_index for r in shard_results] + [
            q.row_index for q in shard_quarantined
        ]
        if sorted(row_indexes) != list(rows):
            raise MergeError(f"shard {shards[i].label} must report every row exactly once")
        shard_facts = [LoanFacts.from_dict(_as_dict(f)) for f in _as_list(sf.get("loans"))]
        identities = Counter((r.row_index, r.loan_id) for r in shard_results)
        if Counter((f.source_row_index, f.loan_id) for f in shard_facts) != identities:
            raise MergeError("loan facts do not correspond to result row identities")
        part = HealthPartial.from_dict(_as_dict(sf.get("health")))
        if (
            part.rows != len(rows)
            or part.loans != len(shard_results)
            or part.quarantined_rows != len(shard_quarantined)
        ):
            raise MergeError("health counters do not correspond to shard rows")
        if Counter(part.diary_truncated_loan_ids) - Counter(r.loan_id for r in shard_results):
            raise MergeError("truncated diary identifiers do not correspond to results")
        if part.diary_records < sum(len(f.paid_rows) for f in shard_facts):
            raise MergeError("paid row facts exceed parsed diary records")
        for key in ("required_columns_missing", "optional_columns_missing"):
            if getattr(part, key) != _meta(doc)[key]:
                raise MergeError("health schema does not match report metadata")
        for key in (
            "columns",
            "required_columns_missing",
            "optional_columns_missing",
            "columns_not_in_data_dictionary",
        ):
            if partials and getattr(part, key) != getattr(partials[0], key):
                raise MergeError("health schema differs between shards")
        results.extend(shard_results)
        quarantined.extend(shard_quarantined)
        facts.extend(shard_facts)
        partials.append(part)

    as_of_source = str(head["as_of_source"])
    tape_checks, tape_findings = evaluate_tape_facts(facts, results, config)
    report = Report(
        input_file=str(head["input_file"]),
        input_sha256=str(head["input_sha256"]),
        rows_read=sum(int(str(_meta(d)["rows_read"])) for d in docs),
        as_of=as_of,
        as_of_source=as_of_source,
        config_digest=config.digest(),
        ruleset_version=str(head["ruleset_version"]),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        schema_ok=True,
        required_columns_missing=[str(c) for c in _as_list(head["required_columns_missing"])],
        optional_columns_missing=[str(c) for c in _as_list(head["optional_columns_missing"])],
        loans=results,
        quarantined=quarantined,
        tape_findings=tape_findings,
        circuit_breaker_tripped=any(t.rule_id == "I3" for t in tape_findings),
        duration_seconds=time.perf_counter() - started,
        tape_checks=tape_checks,
        flagged_profile=flagged_population_profile(results),
        tape_health=finish(combine(partials), results, as_of_source, config),
    )
    return apply_plugins(report, config.plugins)


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise MergeError("expected a nonnegative integer")
    return value


def _as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MergeError(f"malformed shard report: expected a list, got {type(value).__name__}")
    return value


def _as_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MergeError(f"malformed shard report: expected an object, got {type(value).__name__}")
    return value
