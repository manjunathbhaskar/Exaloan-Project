from pathlib import Path

import pytest
from scripts.admission import admit, main

from loan_dq.config import Config
from loan_dq.engine import run
from loan_dq.rules.registry import all_rules
from tests.conftest import SyntheticLoan


def test_high_fire_share_is_diagnostic_not_rejection(tmp_path: Path, config: Config) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Repaid interest"] = 1.0
    path = tmp_path / "tape.csv"
    loan.frame().to_csv(path, index=False)
    stats = admit(run(path, config), set(), 0.4, all_rules())
    assert any(s.notes for s in stats)
    assert not any(s.rejected for s in stats)


def test_minor_clean_anchor_observation_does_not_reject(tmp_path: Path, config: Config) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Purpose"] = "loan_purpose.12"
    path = tmp_path / "tape.csv"
    loan.frame().to_csv(path, index=False)
    stats = admit(run(path, config), {loan.loan_id}, 0.4, all_rules())
    assert not any(s.rejected for s in stats)


@pytest.mark.parametrize("condition", ["missing_clean", "all_quarantined", "schema", "no_rules"])
def test_admission_rejects_unusable_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    loan = SyntheticLoan()
    if condition == "all_quarantined":
        loan.summary_overrides["Loan ID"] = "bad"
    frame = loan.frame()
    if condition == "schema":
        frame = frame.drop(columns=["Loan amount"])
    path = tmp_path / "tape.csv"
    frame.to_csv(path, index=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("as_of: 2025-02-01\n")
    args = ["admission", str(path), "--config", str(cfg)]
    if condition == "missing_clean":
        args.extend(["--clean", "999"])
    if condition == "no_rules":
        cfg.write_text("as_of: 2025-02-01\ndisabled_rules: " + str([r.id for r in all_rules()]))
    monkeypatch.setattr("sys.argv", args)
    assert main() != 0
