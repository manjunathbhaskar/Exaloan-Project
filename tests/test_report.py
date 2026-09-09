"""Report writers and CLI: every loan appears once, normal loans are marked normal, flagged
loans carry reasons, unreadable rows are listed, and no borrower PII leaks into the output."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pandas as pd

from loan_dq.cli import main
from loan_dq.config import Config
from loan_dq.engine import run
from loan_dq.ingest.schema import ingest
from loan_dq.report.health import combine, health_partial
from loan_dq.report.schema import Finding, QuarantineRecord, TapeFinding, loan_tier
from loan_dq.report.writers import _write_frame, write_console, write_csv, write_json
from tests.conftest import SyntheticLoan, make_frame, shift


def _mixed_tape(tmp_path: Path) -> Path:
    clean = SyntheticLoan(loan_id=1)
    late = SyntheticLoan(loan_id=2)
    late.rows[6].paid = shift(late.rows[6].due, 95)
    late.rows[6].state = "paid with delay"
    broken = SyntheticLoan(loan_id=3)
    broken.summary_overrides["Repaid interest"] = 1.0
    unreadable = SyntheticLoan(loan_id=4)
    unreadable.summary_overrides["Loan ID"] = "???"
    unreadable.summary_overrides["City"] = "Zwolle"
    path = tmp_path / "tape.csv"
    make_frame(clean, late, broken, unreadable).to_csv(path, index=False)
    return path


def test_csv_lists_every_loan_including_normal_and_unreadable(
    config: Config, tmp_path: Path
) -> None:
    report = run(_mixed_tape(tmp_path), config)
    out = tmp_path / "summary.csv"
    write_csv(report, out)
    assert b"\r\n" not in out.read_bytes()
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    by_id = {r["loan_id"]: r for r in rows}
    assert set(by_id) == {"1", "2", "3", "[unavailable]"}
    assert by_id["1"]["verdict"] == "normal"
    assert by_id["1"]["primary_reason"] == "no anomalies detected"
    assert by_id["1"]["severity"] == ""
    assert by_id["2"]["verdict"] == "flagged"
    assert "95 days after scheduled date" in by_id["2"]["primary_reason"]
    assert by_id["2"]["credit_event"] == "default"
    assert by_id["3"]["data_integrity"] == "defect"
    assert "[B4]" in by_id["3"]["all_reasons"]
    assert by_id["[unavailable]"]["verdict"] == "unreadable"
    assert by_id["[unavailable]"]["rule_ids"] == "A1"


def test_json_has_reproducibility_header_and_per_loan_detail(
    config: Config, tmp_path: Path
) -> None:
    tape = _mixed_tape(tmp_path)
    report = run(tape, config)
    out = tmp_path / "report.json"
    write_json(report, out)
    doc = json.loads(out.read_text(encoding="utf-8"))
    meta = doc["meta"]
    assert meta["input_file"] == "tape.csv"
    assert len(meta["input_sha256"]) == 64
    assert meta["rows_read"] == 4
    assert meta["as_of"] and meta["as_of_source"] and meta["config_digest"]
    assert meta["ruleset_version"]
    assert meta["schema_ok"] is True
    assert doc["summary"]["loans"] == 3
    assert doc["summary"]["quarantined_rows"] == 1
    assert doc["summary"]["by_verdict"] == {"flagged": 2, "normal": 1}
    assert doc["summary"]["tiers"] == {
        "major": 2,
        "minor": 0,
        "indeterminate": 0,
        "clean": 1,
        "major_share": 0.667,
        "major_or_minor_share": 0.667,
    }
    health = doc["tape_health"]
    assert health["schema"]["rows"] == 4
    assert health["schema"]["quarantined_rows"] == 1
    assert health["schema"]["header_words_with_transposed_letters"] == []
    assert health["dates"]["loan_level"] == {"YYYY-MM-DDThh:mm:ss.sss": 12}
    assert health["dates"]["payments_diary"] == {"DD/MM/YYYY": 192}
    assert health["payments_diary"]["diaries_truncated"] == 0
    assert health["vocabulary"]["untranslated_codes"] == {}
    late = next(loan for loan in doc["loans"] if loan["loan_id"] == 2)
    f1 = next(f for f in late["findings"] if f["rule_id"] == "F1")
    assert f1["evidence"]["worst_delay_days"] == 95
    assert f1["root_cause"] is True
    assert isinstance(late["behaviour_profile"], dict)
    assert late["checks_passed"] > 0
    assert doc["quarantined"][0]["loan_id_raw"] == "[unavailable]"


def test_outputs_contain_no_borrower_pii(config: Config, tmp_path: Path) -> None:
    tape = _mixed_tape(tmp_path)
    report = run(tape, config)
    write_json(report, tmp_path / "r.json")
    write_csv(report, tmp_path / "s.csv")
    buf = io.StringIO()
    write_console(report, buf, verbose=True)
    blob = (
        (tmp_path / "r.json").read_text(encoding="utf-8")
        + (tmp_path / "s.csv").read_text(encoding="utf-8")
        + buf.getvalue()
    )
    for token in ("Zwolle", "Vilnius", "1985", "female", "1800.0", "2500.0"):
        assert token not in blob, token


def test_console_marks_normal_and_lists_reasons(config: Config, tmp_path: Path) -> None:
    report = run(_mixed_tape(tmp_path), config)
    buf = io.StringIO()
    write_console(report, buf, verbose=True)
    text = buf.getvalue()
    assert "1  normal   -        no anomalies detected" in text
    assert "[F1] high:" in text
    assert "unreadable CRITICAL row 3" in text


def test_cli_writes_both_files_and_exits_zero(tmp_path: Path) -> None:
    tape = _mixed_tape(tmp_path)
    out_dir = tmp_path / "out"
    code = main(["run", str(tape), "--out", str(out_dir), "--quiet"])
    assert code == 0
    assert (out_dir / "tape_report.json").exists()
    assert (out_dir / "tape_summary.csv").exists()


def test_cli_missing_required_column_exits_three(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    make_frame(SyntheticLoan()).drop(columns=["payments"]).to_csv(path, index=False)
    code = main(["run", str(path), "--out", str(tmp_path / "out"), "--quiet"])
    assert code == 3
    assert not (tmp_path / "out" / "bad_report.json").exists()


def test_cli_missing_file_exits_two(tmp_path: Path) -> None:
    assert main(["run", str(tmp_path / "nope.xlsx"), "--quiet"]) == 2


def test_cli_as_of_override(tmp_path: Path) -> None:
    tape = _mixed_tape(tmp_path)
    out_dir = tmp_path / "out"
    main(["run", str(tape), "--out", str(out_dir), "--quiet", "--as-of", "2025-03-01"])
    doc = json.loads((out_dir / "tape_report.json").read_text())
    assert doc["meta"]["as_of"] == "2025-03-01"
    assert doc["meta"]["as_of_source"] == "config"


def test_publication_status_and_unknown_coverage(config: Config, tmp_path: Path) -> None:
    tape = tmp_path / "clean.csv"
    make_frame(SyntheticLoan()).to_csv(tape, index=False)
    report = run(tape, config)
    assert report.publication_status == "accepted"
    assert report.to_dict()["meta"]["publication_status"] == "accepted"
    assert report.counts()["publication_status"] == "accepted"
    loan = report.loans[0]
    loan.validation_coverage = "partial"
    loan.findings.append(Finding("R", "x", "integrity", "low", "minor"))
    assert loan_tier(loan) == "indeterminate"
    assert report.publication_status == "review_required"
    loan.validation_coverage = "full"
    report.tape_findings.append(TapeFinding("I1", "critical", "duplicates"))
    assert report.publication_status == "review_required"
    report.tape_findings.clear()
    report.quarantined.append(QuarantineRecord(1, "private-sentinel", "private-sentinel"))
    assert report.publication_status == "review_required"
    assert report.counts()["total_rows"] == 2
    report.schema_ok = False
    assert report.publication_status == "rejected"
    report.schema_ok = True
    report.loans.clear()
    assert report.publication_status == "rejected"


def test_health_allowlist_suppresses_sensitive_and_unexpected_values(
    config: Config, tmp_path: Path
) -> None:
    loans = [SyntheticLoan(loan_id=i) for i in range(12)]
    for loan in loans:
        loan.summary_overrides.update(
            {
                "City": "private-sentinel",
                "Purpose": "private-sentinel",
                "Unexpected": "private-sentinel",
                "Employment status": "private-sentinel",
                "Loan type": "private-sentinel",
                "Days late": 0,
            }
        )
    tape = tmp_path / "privacy.csv"
    make_frame(*loans).to_csv(tape, index=False)
    report = run(tape, config)
    assert report.tape_health is not None
    assert "private-sentinel" not in json.dumps(report.to_dict(), default=str)
    assert report.tape_health.columns.low_information_columns["Days late"].rows_with_it == 12


def test_quarantine_and_error_outputs_do_not_echo_source(config: Config, tmp_path: Path) -> None:
    report = run(_mixed_tape(tmp_path), config)
    report.quarantined[0].loan_id_raw = "=private-sentinel"
    report.quarantined[0].reason = "invalid number 'private-sentinel'"
    write_csv(report, tmp_path / "private.csv")
    buf = io.StringIO()
    write_console(report, buf)
    assert "private-sentinel" not in json.dumps(report.to_dict(), default=str)
    assert "private-sentinel" not in (tmp_path / "private.csv").read_text() + buf.getvalue()
    rows = list(csv.DictReader((tmp_path / "private.csv").open()))
    assert {r["source_row_index"] for r in rows} == {"0", "1", "2", "3"}


def test_csv_formula_escape_applies_only_to_text(tmp_path: Path) -> None:
    frame = pd.DataFrame({"text": ["=1+1", " +cmd", "-cmd", "@cmd"], "number": [-2, 0, 3, 4]})
    path = tmp_path / "escaped.csv"
    _write_frame(frame, path)
    rows = list(csv.DictReader(path.open()))
    assert all(row["text"].startswith("'") for row in rows)
    assert rows[0]["number"] == "-2"


def test_health_empty_strings_json_dates_and_exact_safe_counters(config: Config) -> None:
    synthetic = SyntheticLoan()
    records = [r.as_record(str(synthetic.loan_id)) for r in synthetic.rows]
    synthetic.summary_overrides["payments"] = json.dumps(records)
    frame = make_frame(synthetic)
    frame["City"] = "  "
    frame["Days late"] = 0
    ingested = ingest(frame, config)
    part = health_partial(frame, ingested, config)
    assert part.filled["City"] == 0
    assert part.diary_dates == {"DD/MM/YYYY": len(records) * 2}
    assert set(part.value_counts) == {"Days late", "Loan status", "Loan type"}
    assert "1985" not in json.dumps(part.to_dict())
    many = pd.concat([frame] * 60, ignore_index=True)
    many["Days late"] = list(range(60))
    whole = health_partial(many, ingest(many, config), config)
    parts = [
        health_partial(cut, ingest(cut, config), config) for cut in (many.iloc[:30], many.iloc[30:])
    ]
    merged = combine(parts)
    assert merged.value_counts == whole.value_counts
    assert merged.value_counts_omitted == whole.value_counts_omitted == ["Days late"]


def test_three_check_groups_are_visible_in_json_csv_and_console(
    config: Config, tmp_path: Path
) -> None:
    report = run(_mixed_tape(tmp_path), config)
    doc = report.to_dict()
    groups = {g["id"]: g for g in doc["summary"]["check_groups"]}
    assert {k: g["rule_count"] for k, g in groups.items()} == {
        "record_consistency": 43,
        "repayment_behaviour": 7,
        "whole_tape": 6,
    }
    assert groups["whole_tape"]["scope"] == "tape"
    assert all(t["group"] == "whole_tape" for t in doc["tape_checks"])
    for result in doc["loans"]:
        for finding in result["findings"]:
            assert finding["rule_id"] in groups[finding["group"]]["rule_ids"]
        for outcome in result["rule_outcomes"]:
            assert outcome["rule_id"] in groups[outcome["group"]]["rule_ids"]
    out = tmp_path / "summary.csv"
    write_csv(report, out)
    rows = list(csv.DictReader(out.open()))
    by_id = {r["loan_id"]: r for r in rows}
    assert "repayment_behaviour" in by_id["2"]["finding_groups"].split(";")
    assert "record_consistency" in by_id["3"]["finding_groups"].split(";")
    assert by_id["[unavailable]"]["finding_groups"] == "record_consistency"
    buf = io.StringIO()
    write_console(report, buf, verbose=True)
    assert "Record consistency (43 loan checks)" in buf.getvalue()
    assert "Repayment behaviour (7 loan checks)" in buf.getvalue()
    assert "Whole-tape quality (6 tape checks)" in buf.getvalue()


def test_no_evaluated_rules_are_not_presented_as_group_passes(
    config: Config, tmp_path: Path
) -> None:
    report = run(_mixed_tape(tmp_path), config, rules=[])
    groups = {g["id"]: g for g in report.counts()["check_groups"]}
    assert groups["record_consistency"]["rule_count"] == 0
    assert groups["repayment_behaviour"]["rule_count"] == 0
    assert groups["whole_tape"]["rule_count"] == 6
