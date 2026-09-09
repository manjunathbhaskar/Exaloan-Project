"""Serialise a ``Report`` as JSON (full detail), CSV (one line per loan) and console text, plus
the parsed payment diaries as long-form tables (one row per instalment, one row per loan)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import TextIO

import pandas as pd

from loan_dq.ingest.model import Loan
from loan_dq.report.schema import (
    Report,
    loan_tier,
    public_category,
    public_error,
    public_identifier,
    public_message,
    verdict_tiers,
)
from loan_dq.report.tables import TableContext, diary_coverage_frame, payments_frame
from loan_dq.rules.base import CheckGroup

CSV_COLUMNS = [
    "loan_id",
    "source_row_index",
    "verdict",
    "tier",
    "severity",
    "data_integrity",
    "credit_event",
    "validation_coverage",
    "loan_status",
    "finding_count",
    "finding_groups",
    "rule_ids",
    "primary_reason",
    "all_reasons",
    "checks_passed",
    "checks_failed",
    "checks_not_evaluable",
]


def write_json(report: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")


def write_payments_table(loans: list[Loan], tables: TableContext, path: Path) -> int:
    """Write the exploded diaries; ``.parquet`` when the suffix asks for it, CSV otherwise."""
    frame = payments_frame(loans, tables)
    _write_frame(frame, path)
    return len(frame)


def write_diary_coverage(loans: list[Loan], tables: TableContext, path: Path) -> int:
    frame = diary_coverage_frame(loans, tables)
    _write_frame(frame, path)
    return len(frame)


def _write_frame(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".parquet":
        frame.to_parquet(path, index=False)
    else:
        frame.map(_csv_cell).to_csv(path, index=False, encoding="utf-8", lineterminator="\n")


def _csv_cell(value: object) -> object:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + value
    return value


def _write_csv_row(writer: csv.DictWriter[str], row: dict[str, object]) -> None:
    writer.writerow({k: _csv_cell(v) for k, v in row.items()})


def write_csv(report: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for r in sorted(report.loans, key=lambda x: x.row_index):
            _write_csv_row(
                writer,
                {
                    "loan_id": r.loan_id,
                    "source_row_index": r.row_index,
                    "verdict": r.verdict,
                    "tier": loan_tier(r),
                    "severity": r.severity or "",
                    "data_integrity": r.data_integrity,
                    "credit_event": r.credit_event,
                    "validation_coverage": r.validation_coverage,
                    "loan_status": public_category("Loan status", r.loan_status) or "",
                    "finding_count": len(r.findings),
                    "finding_groups": ";".join(r.finding_groups),
                    "rule_ids": ";".join(f.rule_id for f in r.findings),
                    "primary_reason": r.primary_reason,
                    "all_reasons": " | ".join(
                        f"[{f.rule_id}] {public_message(f.rule_id, f.message)}" for f in r.findings
                    ),
                    "checks_passed": r.checks_passed,
                    "checks_failed": r.checks_failed,
                    "checks_not_evaluable": r.checks_not_evaluable,
                },
            )
        for q in report.quarantined:
            _write_csv_row(
                writer,
                {
                    "loan_id": public_identifier(q.loan_id_raw),
                    "source_row_index": q.row_index,
                    "verdict": "unreadable",
                    "tier": "major",
                    "severity": "critical",
                    "data_integrity": "unknown",
                    "credit_event": "unknown",
                    "validation_coverage": "none",
                    "loan_status": "",
                    "finding_count": 1,
                    "finding_groups": CheckGroup.RECORD_CONSISTENCY.value,
                    "rule_ids": "A1",
                    "primary_reason": f"row could not be parsed: {public_error(q.reason)}",
                    "all_reasons": f"[A1] row could not be parsed: {public_error(q.reason)}",
                    "checks_passed": 0,
                    "checks_failed": 1,
                    "checks_not_evaluable": 0,
                },
            )


def write_console(report: Report, out: TextIO, *, verbose: bool = False) -> None:
    c = report.counts()
    p = out.write
    p(f"loan-dq report  {report.input_file}  sha256={report.input_sha256[:12]}\n")
    p(
        f"as_of={report.as_of.isoformat()} ({report.as_of_source})  "
        f"ruleset={report.ruleset_version}  config={report.config_digest}  "
        f"{report.duration_seconds:.2f}s  publication_status={report.publication_status}\n"
    )
    if report.shard is not None:
        s = report.shard
        p(
            f"shard {s.index}/{s.count}  rows {s.row_start}-{s.row_stop - 1}  "
            "(tape-level checks, tape health and plugins run in 'loan-dq merge')\n"
        )
    if not report.schema_ok:
        p(f"SCHEMA ERROR: required columns missing: {report.required_columns_missing}\n")
        return
    if report.optional_columns_missing:
        p(f"optional columns missing: {report.optional_columns_missing}\n")
    p(
        f"rows={report.rows_read} total_rows={c['total_rows']} loans={c['loans']} "
        f"quarantined={c['quarantined_rows']} "
        f"verdict={c['by_verdict']} severity={c['by_severity']}\n"
    )
    tiers = verdict_tiers(report.loans)
    p(
        f"tiers: {tiers['major']} major (flagged) | {tiers['minor']} minor "
        f"(normal, low observation printed) | {tiers['indeterminate']} indeterminate "
        f"(no finding, but diary truncated/unreadable) | {tiers['clean']} clean  "
        f"-> {tiers['major_share']:.0%} flagged, "
        f"{tiers['major_or_minor_share']:.0%} with any finding\n"
    )
    p(
        f"data_integrity={c['data_integrity']} credit_event={c['credit_event']} "
        f"partial_coverage={c['partial_coverage']}\n"
    )
    p(
        "check groups: "
        + " | ".join(
            f"{g['label']} ({g['rule_count']} {g['scope']} checks)" for g in report.check_groups
        )
        + "\n"
    )
    p(f"rule hits: {c['rule_hits']}\n")
    if report.flagged_profile:
        fp = report.flagged_profile
        p(
            f"flagged profile: by_status={fp['by_loan_status']} by_axis={fp['by_root_axis']} "
            f"by_root_rule={fp['by_root_rule']} "
            f"normal_with_minor_observations={fp['normal_with_minor_observations']}\n"
        )
    for t in report.tape_checks:
        p(f"TAPE [{t.rule_id}] {t.status:<13} {t.message}\n")
    if report.tape_health:
        for line in report.tape_health.lines():
            p(f"HEALTH {line}\n")
    if report.plugins.get("enabled") or report.plugins.get("unknown"):
        p(
            f"plugins: applied={report.plugins.get('applied')} "
            f"unknown={report.plugins.get('unknown')} errors={report.plugins.get('errors')} "
            f"(annotations only; no verdict changed)\n"
        )
    p("\n")
    width = max((len(str(r.loan_id)) for r in report.loans), default=8)
    for r in sorted(report.loans, key=lambda x: x.row_index):
        sev = (r.severity or "-").upper()
        p(
            f"{r.loan_id!s:>{width}}  {r.verdict:<8} {sev:<8} {r.primary_reason} "
            f"(source row {r.row_index})\n"
        )
        if verbose:
            for f in r.findings:
                tag = "" if f.root_cause else f"  (symptom of {f.symptom_of})"
                p(
                    f"{'':>{width}}    [{f.rule_id}] {f.severity}: "
                    f"{public_message(f.rule_id, f.message)}{tag}\n"
                )
    for q in report.quarantined:
        p(
            f"{public_identifier(q.loan_id_raw):>{width}}  unreadable CRITICAL "
            f"row {q.row_index}: {public_error(q.reason)}\n"
        )
