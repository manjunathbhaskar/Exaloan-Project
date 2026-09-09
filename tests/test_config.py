from pathlib import Path

import pytest
from pydantic import ValidationError

from loan_dq.cli import main
from loan_dq.config import Config


@pytest.mark.parametrize(
    "raw",
    [
        {"unknown": 1},
        {"tolerance": {"amount_er": 1}},
        {"schema": {"unknown": []}},
        {"tolerance": {"amount_eur": -1}},
        {"tolerance": {"interest_eur": float("inf")}},
        {"tolerance": {"instalment_interest_rel": float("nan")}},
        {"tape": {"circuit_breaker_share": 1.1}},
        {"lateness": {"habitual_late_share": -0.1}},
        {"lateness": {"default_days": 10}},
        {"lateness": {"drift_min_exceeding": 4}},
        {"lateness": {"trend_min_paid": 0}},
        {"xirr": {"min_inflows": 0}},
        {"xirr": {"low_rate_floor": -1}},
        {"xirr": {"high_rate_ceiling": -0.999}},
        {"plausibility": {"min_age": 101}},
        {"plausibility": {"min_term_months": 0}},
        {"tolerance": {"schedule_min_gap_days": 50}},
        {"verdict": {"flag_min_severity": "urgent"}},
        {"disabled_rules": ["NO_SUCH_RULE"]},
        {"info_only_rules": ["B999"]},
        {"tape": {"benford_min_sample": 0}},
        {"schema": {"required_columns": []}},
        {"schema": {"purpose_code_pattern": "["}},
    ],
)
def test_invalid_config_domain_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_config_rule_ids_match_registry_without_import_cycle() -> None:
    from loan_dq.config import RULE_IDS
    from loan_dq.rules.registry import all_rules

    assert {rule.id for rule in all_rules()} == RULE_IDS


def test_grouped_registry_keeps_all_rules_and_their_stable_identifiers() -> None:
    from loan_dq.rules.base import CheckGroup, check_group
    from loan_dq.rules.registry import all_rules, grouped_rules

    groups = grouped_rules()
    assert {group.value: len(rules) for group, rules in groups.items()} == {
        "record_consistency": 43,
        "repayment_behaviour": 7,
    }
    assert [r.id for r in all_rules()] == [r.id for rules in groups.values() for r in rules]
    assert len({r.id for r in all_rules()}) == 50
    assert all(check_group(r.id) == group for group, rules in groups.items() for r in rules)
    assert check_group("I4") is CheckGroup.WHOLE_TAPE
    assert check_group("C8") is CheckGroup.REPAYMENT_BEHAVIOUR
    assert check_group("A1") is CheckGroup.RECORD_CONSISTENCY
    assert check_group("") is None
    assert check_group("Z1") is None


def test_default_configuration_still_loads() -> None:
    config = Config.load(Path(__file__).resolve().parents[1] / "config" / "default.yaml")
    assert config.digest() == Config().digest()


@pytest.mark.parametrize("text", ["tolerance:\n  amount_eur: -1\n", "schema: [", "[]", "false"])
def test_cli_config_errors_are_concise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(text)
    assert main(["run", str(tmp_path / "unused.csv"), "--config", str(path)]) == 2
    error = capsys.readouterr().err
    assert "config" in error.lower()
    assert "Traceback" not in error
