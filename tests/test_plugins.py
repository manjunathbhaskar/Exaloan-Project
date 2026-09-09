"""Plugin boundary: a plugin may annotate, never decide. Crashing or verdict-mutating plugins
are contained and reported; switching plugins off leaves every verdict byte-identical."""

from __future__ import annotations

import io
import json
from pathlib import Path

from loan_dq.config import Config
from loan_dq.engine import run
from loan_dq.plugins import KNOWN, Annotations, TriagePlugin, apply_plugins
from loan_dq.report.schema import Finding, LoanResult, Report
from loan_dq.report.writers import write_console, write_csv
from tests.conftest import SyntheticLoan, make_frame, shift


def _tape(tmp_path: Path) -> Path:
    clean = SyntheticLoan(loan_id=1)
    late = SyntheticLoan(loan_id=2)
    late.rows[6].paid = shift(late.rows[6].due, 95)
    late.rows[6].state = "paid with delay"
    broken = SyntheticLoan(loan_id=3)
    broken.summary_overrides["Repaid interest"] = 1.0
    path = tmp_path / "tape.csv"
    make_frame(clean, late, broken).to_csv(path, index=False)
    return path


def _verdict_view(report: Report) -> list[dict[str, object]]:
    rows = []
    for r in report.loans:
        d = r.to_dict()
        d.pop("annotations")
        rows.append(d)
    return rows


class VerdictFlipper:
    """A plugin that tries to promote every loan to flagged/critical."""

    name = "flipper"

    def annotate(self, report: Report) -> Annotations:
        for r in report.loans:
            r.verdict = "flagged"
            r.severity = "critical"
        return {r.loan_id: {"note": "promoted"} for r in report.loans}


class Crasher:
    name = "crasher"

    def annotate(self, report: Report) -> Annotations:
        raise RuntimeError("model endpoint unavailable")


def test_plugins_off_by_default_and_verdicts_identical_with_triage_on(
    config: Config, tmp_path: Path
) -> None:
    tape = _tape(tmp_path)
    plain = run(tape, config)
    assert plain.plugins == {"enabled": [], "applied": {}, "unknown": [], "errors": {}}
    assert all(r.annotations == {} for r in plain.loans)

    with_triage = run(tape, config.model_copy(update={"plugins": ["triage"]}))
    assert _verdict_view(with_triage) == _verdict_view(plain)
    assert with_triage.plugins["enabled"] == ["triage"]
    assert with_triage.plugins["errors"] == {}
    annotated = {r.loan_id: r.annotations["triage"] for r in with_triage.loans if r.annotations}
    assert set(annotated) == {2, 3}  # the clean loan carries no finding and gets no rank
    assert annotated[2]["review_rank"] == 1  # 90-day late payer outranks the summary mismatch
    assert annotated[2]["review_queue"] == "major"
    assert with_triage.plugins["applied"] == {"triage": 2}


def _result(
    loan_id: int, verdict: str, severity: str | None, credit: str, roots: int
) -> LoanResult:
    findings = [
        Finding(
            rule_id=f"R{i}",
            category="x",
            axis="integrity",
            severity=severity or "low",
            message="m",
            evidence={},
            root_cause=True,
            symptom_of=None,
        )
        for i in range(roots)
    ]
    return LoanResult(
        loan_id=loan_id,
        row_index=loan_id,
        verdict=verdict,
        severity=severity,
        data_integrity="ok",
        credit_event=credit,
        validation_coverage="full",
        findings=findings,
        rule_outcomes=[],
        checks_passed=0,
        checks_failed=roots,
        checks_not_evaluable=0,
        behaviour_profile=None,
        residuals={},
        loan_status=None,
    )


def test_triage_order_severity_then_credit_then_roots_then_id() -> None:
    results = [
        _result(50, "normal", "low", "none", 1),
        _result(40, "flagged", "high", "none", 2),
        _result(30, "flagged", "high", "default", 1),
        _result(20, "flagged", "critical", "watch", 1),
        _result(10, "flagged", "high", "none", 2),
        _result(60, "normal", None, "none", 0),
    ]
    ordered = sorted(results, key=TriagePlugin.key)
    assert [r.loan_id for r in ordered[:5]] == [20, 30, 10, 40, 50]


def test_verdict_mutating_plugin_is_discarded_and_report_restored(
    config: Config, tmp_path: Path
) -> None:
    report = run(_tape(tmp_path), config)
    before = _verdict_view(report)
    registry = {**KNOWN, VerdictFlipper.name: VerdictFlipper}
    after = apply_plugins(report, ["flipper", "triage"], registry)
    assert _verdict_view(after) == before
    assert "verdict_mutation" in after.plugins["errors"]["flipper"]
    assert all("flipper" not in r.annotations for r in after.loans)
    assert after.plugins["applied"] == {"triage": 2}  # a later plugin still runs


def test_crashing_plugin_is_recorded_and_run_completes(config: Config, tmp_path: Path) -> None:
    report = run(_tape(tmp_path), config)
    before = _verdict_view(report)
    registry = {**KNOWN, Crasher.name: Crasher}
    after = apply_plugins(report, ["crasher"], registry)
    assert _verdict_view(after) == before
    assert after.plugins["errors"]["crasher"] == "RuntimeError: plugin output discarded"
    assert after.plugins["applied"] == {}


def test_unknown_plugin_is_reported_not_guessed(config: Config, tmp_path: Path) -> None:
    report = run(_tape(tmp_path), config.model_copy(update={"plugins": ["llm-summary"]}))
    assert report.plugins["unknown"] == ["llm-summary"]
    assert report.plugins["enabled"] == []


def test_annotations_reach_json_but_not_the_loan_csv(config: Config, tmp_path: Path) -> None:
    report = run(_tape(tmp_path), config.model_copy(update={"plugins": ["triage"]}))
    payload = json.loads(json.dumps(report.to_dict(), default=str))
    assert payload["plugins"]["applied"] == {"triage": 2}
    ranked = next(loan for loan in payload["loans"] if loan["loan_id"] == 2)
    assert ranked["annotations"]["triage"]["review_rank"] == 1

    csv_path = tmp_path / "summary.csv"
    write_csv(report, csv_path)
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert "annotations" not in header and "review_rank" not in header

    buf = io.StringIO()
    write_console(report, buf)
    assert "plugins: applied={'triage': 2}" in buf.getvalue()
    assert "no verdict changed" in buf.getvalue()


def test_plugin_cannot_mutate_provenance_or_caller(config: Config, tmp_path: Path) -> None:
    report = run(_tape(tmp_path), config)
    before = report.to_dict()

    class Mutator:
        def annotate(self, candidate: Report) -> Annotations:
            candidate.input_sha256 = "tampered"
            candidate.tape_health.schema.rows = 999
            candidate.loans[0].residuals["private"] = "private-sentinel"
            return {}

    after = apply_plugins(report, ["mutator"], {"mutator": Mutator})
    assert report.to_dict() == before
    assert after.input_sha256 == report.input_sha256
    assert after.plugins["errors"]
    assert _verdict_view(after) == _verdict_view(report)


def test_plugin_protected_types_are_fingerprinted(config: Config, tmp_path: Path) -> None:
    report = run(_tape(tmp_path), config)

    class Mutator:
        def annotate(self, candidate: Report) -> Annotations:
            candidate.as_of = candidate.as_of.isoformat()
            return {candidate.loans[0].row_index: {"review_rank": 1}}

    after = apply_plugins(report, ["mutator"], {"mutator": Mutator})
    assert after.plugins["errors"]
    assert after.loans[0].annotations == {}


def test_plugin_constructor_validation_and_reference_leaks(config: Config, tmp_path: Path) -> None:
    report = run(_tape(tmp_path), config)

    def broken_constructor():
        raise ValueError("private-sentinel")

    class Invalid:
        def annotate(self, candidate: Report):
            return {candidate.loans[0].row_index: {"bad": object()}}

    payload = {"review_rank": 1}

    class Valid:
        def annotate(self, candidate: Report) -> Annotations:
            return {candidate.loans[0].row_index: payload}

    after = apply_plugins(
        report,
        ["ctor", "invalid", "valid"],
        {"ctor": broken_constructor, "invalid": Invalid, "valid": Valid},
    )
    assert set(after.plugins["errors"]) == {"ctor", "invalid"}
    assert "private-sentinel" not in json.dumps(after.to_dict(), default=str)
    payload["review_rank"] = 9
    assert after.loans[0].annotations["valid"] == {"review_rank": 1}
    assert report.loans[0].annotations == {}


def test_triage_uses_source_row_identity(config: Config, tmp_path: Path) -> None:
    report = run(_tape(tmp_path), config)
    report.loans[0].loan_id = report.loans[1].loan_id
    after = apply_plugins(report, ["triage"])
    assert after.loans[0].annotations == {}
    assert after.loans[1].annotations["triage"]["review_rank"] == 1
