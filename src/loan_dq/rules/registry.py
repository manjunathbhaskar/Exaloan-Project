"""The admitted rule set, in evaluation order. ``RULESET_VERSION`` is stamped into reports."""

from __future__ import annotations

from loan_dq.rules import (
    arithmetic,
    behaviour,
    identities,
    lateness,
    lifecycle,
    plausibility,
    readability,
    temporal,
)
from loan_dq.rules.base import CheckGroup, Rule, check_group

RULESET_VERSION = "2026.09.3"


def grouped_rules() -> dict[CheckGroup, list[Rule]]:
    rules: list[Rule] = [
        *readability.RULES,
        *identities.RULES,
        *arithmetic.RULES,
        *temporal.RULES,
        *lifecycle.RULES,
        *lateness.RULES,
        *behaviour.RULES,
        *plausibility.RULES,
    ]
    groups: dict[CheckGroup, list[Rule]] = {
        CheckGroup.RECORD_CONSISTENCY: [],
        CheckGroup.REPAYMENT_BEHAVIOUR: [],
    }
    for rule in rules:
        group = check_group(rule.id)
        if group is None or group is CheckGroup.WHOLE_TAPE:
            raise RuntimeError("loan rule has no valid inspection group")
        groups[group].append(rule)
    return groups


def all_rules() -> list[Rule]:
    rules = [rule for members in grouped_rules().values() for rule in members]
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise RuntimeError(f"duplicate rule ids in registry: {dupes}")
    return rules
