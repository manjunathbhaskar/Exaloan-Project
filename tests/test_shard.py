"""Sharding: ``run --shard i/n`` on row cuts, then ``merge``, must equal one full run.

Per-loan verdicts are independent by construction, so the shard cut is an I/O detail; the
tape-level checks (I1-I4) and tape health are the only parts that need the whole tape and they
run exactly once, in the merge. These tests pin that composition on the real workbook and on
synthetic tapes with a duplicate loan ID and a quarantined row straddling shard boundaries.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from loan_dq.cli import main
from loan_dq.config import Config
from loan_dq.engine import ShardError, plan_shards, run_pipeline, shard_bounds
from loan_dq.io import reader
from loan_dq.io.reader import count_rows, read_tape
from loan_dq.report.schema import Report
from loan_dq.rules.tape import LoanFacts
from loan_dq.shard import MergeError, load_report_dict, merge_reports
from tests.conftest import WORKBOOK, SyntheticLoan, make_frame

AS_OF = date(2025, 1, 23)


def _json_roundtrip(report: Report) -> dict[str, object]:
    doc = json.loads(json.dumps(report.to_dict(), default=str))
    assert isinstance(doc, dict)
    return doc


def _shard_and_merge(tape: Path, config: Config, count: int) -> tuple[Report, list[Report]]:
    pinned = config.model_copy(update={"as_of": AS_OF})
    shards = [run_pipeline(tape, pinned, shard=(i, count)).report for i in range(1, count + 1)]
    return merge_reports([_json_roundtrip(s) for s in shards], config), shards


def _comparable(report: Report) -> dict[str, object]:
    doc = report.to_dict()
    meta = doc["meta"]
    assert isinstance(meta, dict)
    for volatile in ("generated_at", "duration_seconds"):
        meta.pop(volatile, None)
    return doc


# --- the cut -------------------------------------------------------------------------------


@pytest.mark.parametrize(("rows", "count"), [(72, 4), (72, 7), (10, 3), (1, 1), (5, 5), (3, 8)])
def test_shard_bounds_tile_the_tape_exactly_once(rows: int, count: int) -> None:
    ranges = [shard_bounds(rows, i, count) for i in range(1, count + 1)]
    covered = [r for rng in ranges for r in rng]
    assert covered == list(range(rows))
    assert max(len(r) for r in ranges) - min(len(r) for r in ranges) <= 1
    assert ranges == [shard_bounds(rows, i, count) for i in range(1, count + 1)]


@pytest.mark.parametrize(("index", "count"), [(0, 4), (5, 4), (1, 0), (-1, 2)])
def test_shard_bounds_reject_out_of_range_spec(index: int, count: int) -> None:
    with pytest.raises(ShardError):
        shard_bounds(72, index, count)


def test_csv_row_range_read_matches_whole_read_across_chunk_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tape = tmp_path / "tape.csv"
    loans = [SyntheticLoan(loan_id=100 + i, borrower_id=i) for i in range(11)]
    make_frame(*loans).to_csv(tape, index=False)
    monkeypatch.setattr(reader, "CSV_CHUNK_ROWS", 3)
    whole = read_tape(tape)
    assert count_rows(tape) == 11
    for rng in (range(0, 4), range(2, 7), range(7, 11), range(10, 11)):
        part = read_tape(tape, rows=rng)
        pd.testing.assert_frame_equal(part, whole.iloc[rng.start : rng.stop].reset_index(drop=True))


def test_shard_needs_a_pinned_as_of(config: Config, tmp_path: Path) -> None:
    tape = tmp_path / "tape.csv"
    make_frame(SyntheticLoan(loan_id=1), SyntheticLoan(loan_id=2)).to_csv(tape, index=False)
    assert config.as_of is None
    with pytest.raises(ShardError, match="as-of"):
        run_pipeline(tape, config, shard=(1, 2))


def test_empty_shard_is_an_error_not_an_empty_report(config: Config, tmp_path: Path) -> None:
    tape = tmp_path / "tape.csv"
    make_frame(SyntheticLoan(loan_id=1), SyntheticLoan(loan_id=2)).to_csv(tape, index=False)
    with pytest.raises(ShardError, match="no rows"):
        run_pipeline(tape, config.model_copy(update={"as_of": AS_OF}), shard=(3, 3))


# --- shards + merge == full run --------------------------------------------------------------


@pytest.mark.skipif(not WORKBOOK.exists(), reason="data/loans.xlsx not present")
@pytest.mark.parametrize("count", [1, 4, 7])
def test_workbook_shards_merge_to_the_full_report(config: Config, count: int) -> None:
    full = run_pipeline(WORKBOOK, config.model_copy(update={"as_of": AS_OF})).report
    merged, shards = _shard_and_merge(WORKBOOK, config, count)

    assert [s.shard.label for s in shards if s.shard] == [
        f"{i}of{count}" for i in range(1, count + 1)
    ]
    assert all(s.tape_checks == [] and s.tape_health is None for s in shards)
    assert sum(len(s.loans) for s in shards) == 72
    assert merged.shard is None and merged.shard_facts is None
    assert _comparable(merged) == _comparable(full)


@pytest.mark.skipif(not WORKBOOK.exists(), reason="data/loans.xlsx not present")
def test_plugins_run_once_in_the_merge_and_match_the_full_run(config: Config) -> None:
    with_triage = config.model_copy(update={"plugins": ["triage"]})
    full = run_pipeline(WORKBOOK, with_triage.model_copy(update={"as_of": AS_OF})).report
    merged, shards = _shard_and_merge(WORKBOOK, with_triage, 3)
    assert all(s.plugins == {} for s in shards)
    assert all(r.annotations == {} for s in shards for r in s.loans)
    assert merged.plugins == full.plugins
    assert [r.annotations for r in merged.loans] == [r.annotations for r in full.loans]


def _tape_with_cross_shard_facts(path: Path) -> None:
    """Six rows: a duplicate loan ID in rows 0 and 5, a quarantined row at 3, a 95 %-constant
    column - each fact only visible once the shards are put back together."""
    loans = [SyntheticLoan(loan_id=7001 + i, borrower_id=1 + i) for i in range(6)]
    loans[5].loan_id = 7001
    loans[3].summary_overrides["Loan ID"] = "not-an-id"
    make_frame(*loans).to_csv(path, index=False)


def test_tape_level_facts_span_shard_boundaries(config: Config, tmp_path: Path) -> None:
    tape = tmp_path / "tape.csv"
    _tape_with_cross_shard_facts(tape)
    full = run_pipeline(tape, config.model_copy(update={"as_of": AS_OF})).report
    merged, shards = _shard_and_merge(tape, config, 3)

    assert [q.row_index for q in merged.quarantined] == [3]
    assert [s.shard.row_start for s in shards if s.shard] == [0, 2, 4]
    assert all(s.tape_findings == [] for s in shards)
    i1 = next(c for c in merged.tape_checks if c.rule_id == "I1")
    assert i1.status == "fail" and 7001 in i1.evidence["duplicate_loan_ids"]
    assert _comparable(merged) == _comparable(full)


# --- merge refuses inconsistent input ------------------------------------------------------


def _shard_docs(tape: Path, config: Config, count: int) -> list[dict[str, object]]:
    pinned = config.model_copy(update={"as_of": AS_OF})
    return [
        _json_roundtrip(run_pipeline(tape, pinned, shard=(i, count)).report)
        for i in range(1, count + 1)
    ]


def test_merge_rejects_missing_duplicate_or_foreign_shards(config: Config, tmp_path: Path) -> None:
    tape = tmp_path / "tape.csv"
    _tape_with_cross_shard_facts(tape)
    docs = _shard_docs(tape, config, 3)
    with pytest.raises(MergeError, match="missing \\[2\\]"):
        merge_reports([docs[0], docs[2]], config)
    with pytest.raises(MergeError, match="exactly once"):
        merge_reports([*docs, docs[1]], config)
    with pytest.raises(MergeError, match="not a shard report"):
        merge_reports([_json_roundtrip(run_pipeline(tape, config).report)], config)
    other = tmp_path / "other.csv"
    make_frame(*(SyntheticLoan(loan_id=i) for i in (1, 2, 3))).to_csv(other, index=False)
    foreign = _shard_docs(other, config, 3)
    with pytest.raises(MergeError, match="input_sha256"):
        merge_reports([docs[0], docs[1], foreign[2]], config)


def test_merge_rejects_a_different_config(config: Config, tmp_path: Path) -> None:
    tape = tmp_path / "tape.csv"
    _tape_with_cross_shard_facts(tape)
    docs = _shard_docs(tape, config, 2)
    loosened = config.model_copy(
        update={"lateness": config.lateness.model_copy(update={"grace_days": 6})}
    )
    with pytest.raises(MergeError, match="config differs"):
        merge_reports(docs, loosened)


# --- the command line ----------------------------------------------------------------------


def test_cli_plan_run_shards_merge(
    config: Config, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tape = tmp_path / "tape.csv"
    _tape_with_cross_shard_facts(tape)
    out = tmp_path / "out"

    assert main(["plan", str(tape), "--shards", "3"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["rows"] == 6
    assert [s["rows"] for s in plan["shards"]] == [[0, 2], [2, 4], [4, 6]]
    assert plan["as_of_source"].startswith("inferred")
    assert [asdict(s) for s in plan_shards(tape, 3)] == [
        {"index": i, "count": 3, "row_start": 2 * (i - 1), "row_stop": 2 * i} for i in (1, 2, 3)
    ]

    for i in (1, 2, 3):
        argv = ["run", str(tape), "--shard", f"{i}/3", "--as-of", plan["as_of"], "--out", str(out)]
        assert main([*argv, "--quiet", "--tables", "none"]) == 0
    written = sorted(p.name for p in out.glob("*_report.json"))
    assert written == [f"tape.shard-{i}of3_report.json" for i in (1, 2, 3)]
    shard_doc = load_report_dict(out / "tape.shard-2of3_report.json")
    meta = shard_doc["meta"]
    assert isinstance(meta, dict) and meta["shard"] == {
        "index": 2,
        "count": 3,
        "row_start": 2,
        "row_stop": 4,
    }
    assert "shard_facts" in shard_doc

    merged_dir = tmp_path / "merged"
    assert main(["merge", *map(str, out.glob("*_report.json")), "--out", str(merged_dir)]) == 0
    console = capsys.readouterr().out
    assert "TAPE [I1] fail" in console
    merged_doc = json.loads((merged_dir / "tape_report.json").read_text())
    assert merged_doc["meta"]["shard"] is None
    assert "shard_facts" not in merged_doc
    assert merged_doc["meta"]["rows_read"] == 6
    assert merged_doc["meta"]["as_of"] == plan["as_of"]

    full_dir = tmp_path / "full"
    assert main(["run", str(tape), "--as-of", plan["as_of"], "--out", str(full_dir), "-q"]) == 0
    full_doc = json.loads((full_dir / "tape_report.json").read_text())
    for doc in (merged_doc, full_doc):
        for volatile in ("generated_at", "duration_seconds"):
            doc["meta"].pop(volatile)
    assert merged_doc == full_doc


def test_cli_shard_without_as_of_exits_two(tmp_path: Path) -> None:
    tape = tmp_path / "tape.csv"
    make_frame(SyntheticLoan(loan_id=1), SyntheticLoan(loan_id=2)).to_csv(tape, index=False)
    assert main(["run", str(tape), "--shard", "1/2", "--out", str(tmp_path / "o"), "-q"]) == 2


def test_cli_rejects_malformed_shard_spec(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["run", str(tmp_path / "t.csv"), "--shard", "3/2"])
    with pytest.raises(SystemExit):
        main(["run", str(tmp_path / "t.csv"), "--shard", "half"])


@pytest.mark.parametrize(
    "defect",
    ["missing", "duplicate", "quarantine", "facts", "health", "version", "start", "stop", "shape"],
)
def test_merge_rejects_malformed_row_coverage(config: Config, tmp_path: Path, defect: str) -> None:
    tape = tmp_path / "tape.csv"
    _tape_with_cross_shard_facts(tape)
    docs = _shard_docs(tape, config, 3)
    if defect == "missing":
        docs[0]["loans"].pop()
    elif defect == "duplicate":
        docs[0]["loans"][1] = docs[0]["loans"][0]
    elif defect == "quarantine":
        docs[1]["quarantined"][0]["row_index"] = 1
    elif defect == "facts":
        docs[0]["shard_facts"]["loans"].pop()
    elif defect == "health":
        docs[0]["shard_facts"]["health"]["loans"] = 99
    elif defect == "version":
        for doc in docs:
            doc["meta"]["ruleset_version"] = "obsolete"
    elif defect == "start":
        docs[0]["meta"]["shard"]["row_start"] = -1
    elif defect == "stop":
        docs[-1]["meta"]["shard"]["row_stop"] += 1
    else:
        docs[0]["meta"] = []
    with pytest.raises(MergeError):
        merge_reports(docs, config)


def test_shard_borrower_token_uses_configured_lender(config: Config, tmp_path: Path) -> None:
    tape = tmp_path / "tape.csv"
    make_frame(SyntheticLoan(loan_id=1), SyntheticLoan(loan_id=2)).to_csv(tape, index=False)
    pinned = config.model_copy(update={"as_of": AS_OF, "lender": "lender-a"})
    outcome = run_pipeline(tape, pinned, shard=(1, 2))
    fact = outcome.report.to_dict()["shard_facts"]["loans"][0]
    expected = LoanFacts.from_loan(outcome.ingested.loans[0], lender_scope="lender-a").to_dict()
    assert fact == expected
    assert not {"borrower_id", "birth_year", "borrower_type"} & fact.keys()


def test_merge_empty_documents_translates_errors(config: Config) -> None:
    for docs in ([], [{}], [{"meta": {"shard": {}}}]):
        with pytest.raises(MergeError):
            merge_reports(docs, config)


@pytest.mark.parametrize("field", ["findings", "rule_outcomes", "finding_groups"])
def test_merge_rejects_mislabelled_check_groups(config: Config, tmp_path: Path, field: str) -> None:
    tape = tmp_path / "tape.csv"
    broken = SyntheticLoan(loan_id=1)
    broken.summary_overrides["Repaid interest"] = 1.0
    make_frame(broken, SyntheticLoan(loan_id=2)).to_csv(tape, index=False)
    docs = _shard_docs(tape, config, 2)
    result = docs[0]["loans"][0]
    if field == "finding_groups":
        result[field] = ["whole_tape"]
    else:
        result[field][0]["group"] = "whole_tape"
    with pytest.raises(MergeError, match="group"):
        merge_reports(docs, config)
