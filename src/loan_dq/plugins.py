"""Opt-in post-processors that annotate a finished ``Report`` without touching a verdict.

The deterministic rules are the only thing allowed to decide ``verdict``, ``severity``, the
findings and the coverage of a loan. A plugin - a review-ordering heuristic today, an LLM
ranker or summariser tomorrow - receives the finished report and returns annotations keyed by
loan ID. Three guards make that boundary real rather than a convention:

* a plugin exception is caught and recorded; the run and every verdict are unaffected;
* the verdict-bearing part of the report is fingerprinted before and after each plugin - if a
  plugin mutated it, the report is restored from a snapshot and the plugin's output discarded;
* unknown plugin names are reported, not guessed.

Switching every plugin off therefore changes no verdict, no severity and no reason string,
which is what makes a plugin's output measurable against the rules it sits on.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date
from typing import Protocol

from loan_dq.report.schema import LoanResult, Report, loan_tier
from loan_dq.rules.base import Severity

log = logging.getLogger("loan_dq.plugins")

Annotations = dict[int, dict[str, object]]


class Plugin(Protocol):
    name: str

    def annotate(self, report: Report) -> Annotations:
        """Return per-loan annotations; must not modify ``report``."""
        ...


@dataclass
class PluginReport:
    enabled: list[str] = field(default_factory=list)
    applied: dict[str, int] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_CREDIT_ORDER = {"default": 0, "watch": 1, "none": 2, "unknown": 3}


class TriagePlugin:
    """Deterministic review order for loans carrying a finding: severity, then credit event,
    then number of independent root causes, then loan ID. A learned ranker would replace the
    key function and nothing else; the fallback is this order."""

    name = "triage"
    basis = "severity desc > credit event (default, watch, none) > root causes desc > loan ID"

    @staticmethod
    def key(result: LoanResult) -> tuple[int, int, int, int, int]:
        severity = Severity(result.severity).rank if result.severity else 0
        roots = sum(1 for f in result.findings if f.root_cause and f.severity != "info")
        return (
            -severity,
            _CREDIT_ORDER.get(result.credit_event, 3),
            -roots,
            result.loan_id,
            result.row_index,
        )

    def annotate(self, report: Report) -> Annotations:
        queue = [r for r in report.loans if any(f.severity != "info" for f in r.findings)]
        queue.sort(key=self.key)
        return {
            r.row_index: {
                "review_rank": rank,
                "review_queue": loan_tier(r),
                "basis": self.basis,
            }
            for rank, r in enumerate(queue, start=1)
        }


KNOWN: dict[str, Callable[[], Plugin]] = {TriagePlugin.name: TriagePlugin}


def _fingerprint(report: Report) -> str:
    """Everything a consumer could act on, minus the annotations plugins are allowed to add."""

    def typed(value: object) -> object:
        if is_dataclass(value) and not isinstance(value, type):
            fields = dict(vars(value))
            if isinstance(value, LoanResult):
                fields.pop("annotations", None)
            return [type(value).__qualname__, typed(fields)]
        if isinstance(value, dict):
            pairs = [[typed(k), typed(v)] for k, v in value.items()]
            return ["dict", sorted(pairs, key=lambda pair: json.dumps(pair[0]))]
        if isinstance(value, (list, tuple)):
            return [type(value).__name__, [typed(v) for v in value]]
        if isinstance(value, date):
            return [type(value).__name__, value.isoformat()]
        if value is None or type(value) in (str, bool, int, float):
            return [type(value).__name__, value]
        raise TypeError("unsupported protected report value")

    return json.dumps(typed(report), sort_keys=True, allow_nan=False)


def _validate_annotations(annotations: object, report: Report) -> Annotations:
    def valid(value: object) -> bool:
        if value is None or type(value) in (str, int, bool):
            return True
        if type(value) is float:
            return math.isfinite(value)
        if type(value) is list:
            return all(valid(v) for v in value)
        if type(value) is dict:
            return all(type(k) is str and valid(v) for k, v in value.items())
        return False

    rows = {r.row_index for r in report.loans}
    if len(rows) != len(report.loans) or type(annotations) is not dict:
        raise ValueError("invalid annotation mapping")
    for key, value in annotations.items():
        if type(key) is not int or key not in rows or type(value) is not dict or not valid(value):
            raise ValueError("invalid annotation value")
    return copy.deepcopy(annotations)


def apply_plugins(
    report: Report,
    names: Sequence[str],
    registry: Mapping[str, Callable[[], Plugin]] = KNOWN,
) -> Report:
    report = copy.deepcopy(report)
    status = PluginReport()
    for name in names:
        factory = registry.get(name)
        if factory is None:
            status.unknown.append(name)
            log.warning("plugin %r is not registered; ignored", name)
            continue
        status.enabled.append(name)
        try:
            candidate = copy.deepcopy(report)
            before = _fingerprint(candidate)
            plugin = factory()
            annotations = plugin.annotate(candidate)
            if _fingerprint(candidate) != before:
                log.error("plugin %s modified protected data; output discarded", name)
                status.errors[name] = "verdict_mutation: output discarded, report restored"
                continue
            annotations = _validate_annotations(annotations, report)
        except Exception as exc:  # an optional layer must never take the verdicts down with it
            log.error("plugin %s failed; its output is discarded", name)
            status.errors[name] = f"{type(exc).__name__}: plugin output discarded"
            continue
        applied = 0
        for r in report.loans:
            annotation = annotations.get(r.row_index)
            if annotation is not None:
                r.annotations[name] = annotation
                applied += 1
        status.applied[name] = applied
    report.plugins = status.to_dict()
    return report
