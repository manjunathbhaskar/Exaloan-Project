"""Output contract: what one loan's verdict and the tape summary look like.

This is the only shape downstream consumers (CSV/JSON writers, Part 2's persistence layer,
a future ML triage model) depend on.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from loan_dq.rules.base import Axis, CheckGroup, Severity, Status, check_group

if TYPE_CHECKING:
    from loan_dq.report.health import TapeHealth


PUBLIC_CATEGORIES = {
    "Loan status": frozenset({"granted", "repaid", "terminated"}),
    "Loan type": frozenset({"instalment", "deferred annuity"}),
}


def public_category(field_name: str, value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if text in PUBLIC_CATEGORIES.get(field_name, ()) else "unknown"


def public_identifier(value: object) -> str:
    text = str(value)
    return text if re.fullmatch(r"[0-9]{1,20}", text) else "[unavailable]"


def public_error(value: object) -> str | None:
    return "input could not be parsed; inspect the original source" if value else None


def public_message(rule_id: str, message: str) -> str:
    if message.startswith("rule_error"):
        return "rule_error: evaluation failed; inspect the original source"
    if message == "business borrower":
        return "not applicable for this borrower type"
    if rule_id == "H3" and message.startswith("Monthly payment is "):
        return "Monthly payment exceeds the configured income share; review affordability"
    if rule_id == "H4" and "borrower" in message and "column missing" not in message:
        return "borrower demographic fields are inconsistent"
    if rule_id == "H10" and message.startswith("employment status"):
        return "employment and income fields merit contextual review"
    replacements = {
        "A6": "categorical fields contain missing, unknown or untranslated values",
        "A7": "payment diary is empty or unreadable; inspect the original source",
        "A9": "payment records contain malformed fields; inspect the original source",
        "A10": "payment diary contains records with a different loan identifier",
    }
    return replacements.get(rule_id, message)


@dataclass
class Finding:
    rule_id: str
    category: str
    axis: str
    severity: str
    message: str
    evidence: dict[str, object] = field(default_factory=dict)
    root_cause: bool = True
    symptom_of: str | None = None

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["group"] = group.value if (group := check_group(self.rule_id)) else None
        d["message"] = public_message(self.rule_id, self.message)
        evidence = dict(self.evidence)
        if self.rule_id in {"A3", "A4", "A6", "A7", "A9", "A10"}:
            evidence = {
                k: v
                for k, v in evidence.items()
                if k in {"fields", "raw_length", "bad_records", "expected"}
            }
            fields = evidence.get("fields")
            if isinstance(fields, dict):
                evidence["fields"] = list(fields)
        if self.rule_id == "H3":
            evidence = {k: v for k, v in evidence.items() if k in {"limit", "confidence"}}
            evidence["fields"] = ["Monthly payment", "income"]
        if "loan_status" in evidence:
            evidence["loan_status"] = public_category("Loan status", evidence["loan_status"])
        d["evidence"] = evidence
        return d


@dataclass
class RuleOutcome:
    rule_id: str
    status: str
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "group": group.value if (group := check_group(self.rule_id)) else None,
            "note": public_message(self.rule_id, self.note) if self.note else "",
        }


@dataclass
class LoanResult:
    loan_id: int
    row_index: int
    verdict: str  # normal | flagged | unreadable
    severity: str | None
    data_integrity: str  # clean | defect | unknown
    credit_event: str  # none | watch | default
    validation_coverage: str  # full | partial | none
    findings: list[Finding]
    rule_outcomes: list[RuleOutcome]
    checks_passed: int
    checks_failed: int
    checks_not_evaluable: int
    behaviour_profile: dict[str, object] | None
    residuals: dict[str, object]
    loan_status: str | None
    annotations: dict[str, object] = field(default_factory=dict)  # plugin output, never a verdict

    @property
    def primary_reason(self) -> str:
        roots = [f for f in self.findings if f.root_cause and f.severity != Severity.INFO.value]
        if roots:
            # On equal severity the credit event (the brief's headline anomaly) leads the summary.
            lead = max(roots, key=lambda f: (Severity(f.severity).rank, f.axis == "credit"))
            if self.verdict == "normal":
                prefix = (
                    "no major issues; minor observation"
                    if self.validation_coverage == "full"
                    else "validation incomplete; observation"
                )
                return f"{prefix}: {public_message(lead.rule_id, lead.message)}"
            return public_message(lead.rule_id, lead.message)
        if self.findings:
            first = self.findings[0]
            return public_message(first.rule_id, first.message)
        return (
            "no anomalies detected"
            if self.validation_coverage == "full"
            else "validation incomplete; absence of findings does not establish a clean loan"
        )

    @property
    def finding_groups(self) -> list[str]:
        present = {check_group(f.rule_id) for f in self.findings}
        return [group.value for group in CheckGroup if group in present]

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["source_row_index"] = self.row_index
        d["loan_status"] = public_category("Loan status", self.loan_status)
        d["findings"] = [f.to_dict() for f in self.findings]
        d["finding_groups"] = self.finding_groups
        d["rule_outcomes"] = [o.to_dict() for o in self.rule_outcomes]
        d["tier"] = loan_tier(self)
        d["primary_reason"] = self.primary_reason
        d["rule_ids"] = [f.rule_id for f in self.findings]
        return d


def loan_tier(r: LoanResult) -> str:
    """major = flagged; minor = normal with a low observation; indeterminate = nothing but
    info, yet the diary was truncated or unreadable so absence of a finding is not evidence of
    a clean loan; clean = nothing but info on a fully validated loan."""
    if r.verdict == "flagged":
        return "major"
    if r.validation_coverage != "full":
        return "indeterminate"
    if any(f.severity != "info" for f in r.findings):
        return "minor"
    return "clean"


def verdict_tiers(results: list[LoanResult]) -> dict[str, object]:
    counts = {"major": 0, "minor": 0, "indeterminate": 0, "clean": 0}
    for r in results:
        counts[loan_tier(r)] += 1
    n = len(results) or 1
    return {
        **counts,
        "major_share": round(counts["major"] / n, 3),
        "major_or_minor_share": round((counts["major"] + counts["minor"]) / n, 3),
    }


@dataclass
class QuarantineRecord:
    row_index: int
    loan_id_raw: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "row_index": self.row_index,
            "source_row_index": self.row_index,
            "loan_id_raw": public_identifier(self.loan_id_raw),
            "reason": public_error(self.reason),
        }


@dataclass
class TapeFinding:
    rule_id: str
    severity: str
    message: str
    evidence: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "group": CheckGroup.WHOLE_TAPE.value}


@dataclass
class TapeCheck:
    """Outcome of one tape-level check, recorded whether or not it fired."""

    rule_id: str
    title: str
    status: str  # pass | fail | not_evaluable
    severity: str
    message: str
    evidence: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "group": CheckGroup.WHOLE_TAPE.value}


@dataclass
class ShardInfo:
    """Which slice of the tape a shard report covers (``index`` is 1-based, rows 0-based)."""

    index: int
    count: int
    row_start: int
    row_stop: int

    @property
    def label(self) -> str:
        return f"{self.index}of{self.count}"

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> ShardInfo:
        return cls(*(int(str(d[k])) for k in ("index", "count", "row_start", "row_stop")))


@dataclass
class Report:
    input_file: str
    input_sha256: str
    rows_read: int
    as_of: date
    as_of_source: str
    config_digest: str
    ruleset_version: str
    generated_at: str
    schema_ok: bool
    required_columns_missing: list[str]
    optional_columns_missing: list[str]
    loans: list[LoanResult]
    quarantined: list[QuarantineRecord]
    tape_findings: list[TapeFinding]
    circuit_breaker_tripped: bool
    duration_seconds: float
    tape_checks: list[TapeCheck] = field(default_factory=list)
    flagged_profile: dict[str, object] = field(default_factory=dict)
    tape_health: TapeHealth | None = None
    plugins: dict[str, object] = field(default_factory=dict)
    shard: ShardInfo | None = None
    # Per-loan facts the tape-level checks and tape health need; carried out of a shard so that
    # ``merge`` can evaluate them once over the whole tape. Absent on a full or merged report.
    shard_facts: dict[str, object] | None = None

    @property
    def publication_status(self) -> str:
        if not self.schema_ok or not self.loans:
            return "rejected"
        if (
            self.quarantined
            or any(r.validation_coverage != "full" for r in self.loans)
            or self.circuit_breaker_tripped
            or any(t.severity in {"high", "critical"} for t in self.tape_findings)
            or self.shard is not None
            or self.rows_read != len(self.loans) + len(self.quarantined)
        ):
            return "review_required"
        return "accepted"

    @property
    def check_groups(self) -> list[dict[str, object]]:
        observed = {o.rule_id for loan in self.loans for o in loan.rule_outcomes}
        observed.update(t.rule_id for t in self.tape_checks)
        groups = {
            group: sorted(r for r in observed if check_group(r) == group) for group in CheckGroup
        }
        return [
            {
                "id": group.value,
                "label": group.label,
                "scope": "tape" if group is CheckGroup.WHOLE_TAPE else "loan",
                "rule_count": len(rule_ids),
                "rule_ids": rule_ids,
            }
            for group, rule_ids in groups.items()
        ]

    def counts(self) -> dict[str, object]:
        by_verdict: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_rule: dict[str, int] = {}
        integrity: dict[str, int] = {}
        credit: dict[str, int] = {}
        for r in self.loans:
            by_verdict[r.verdict] = by_verdict.get(r.verdict, 0) + 1
            if r.severity:
                by_severity[r.severity] = by_severity.get(r.severity, 0) + 1
            integrity[r.data_integrity] = integrity.get(r.data_integrity, 0) + 1
            credit[r.credit_event] = credit.get(r.credit_event, 0) + 1
            for f in r.findings:
                by_rule[f.rule_id] = by_rule.get(f.rule_id, 0) + 1
        return {
            "publication_status": self.publication_status,
            "rows_read": self.rows_read,
            "total_rows": len(self.loans) + len(self.quarantined),
            "unaccounted_rows": self.rows_read - len(self.loans) - len(self.quarantined),
            "row_outcomes": {
                **dict(sorted(by_verdict.items())),
                "quarantined": len(self.quarantined),
            },
            "validation_coverage": {
                coverage: sum(r.validation_coverage == coverage for r in self.loans)
                for coverage in sorted({r.validation_coverage for r in self.loans})
            },
            "loans": len(self.loans),
            "quarantined_rows": len(self.quarantined),
            "tiers": verdict_tiers(self.loans),
            "by_verdict": dict(sorted(by_verdict.items())),
            "by_severity": dict(sorted(by_severity.items(), key=lambda kv: Severity(kv[0]).rank)),
            "data_integrity": dict(sorted(integrity.items())),
            "credit_event": dict(sorted(credit.items())),
            "partial_coverage": sum(1 for r in self.loans if r.validation_coverage == "partial"),
            "rule_hits": dict(sorted(by_rule.items())),
            "check_groups": self.check_groups,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "meta": {
                "input_file": self.input_file,
                "input_sha256": self.input_sha256,
                "rows_read": self.rows_read,
                "as_of": self.as_of.isoformat(),
                "as_of_source": self.as_of_source,
                "config_digest": self.config_digest,
                "ruleset_version": self.ruleset_version,
                "generated_at": self.generated_at,
                "duration_seconds": round(self.duration_seconds, 3),
                "schema_ok": self.schema_ok,
                "publication_status": self.publication_status,
                "required_columns_missing": self.required_columns_missing,
                "optional_columns_missing": self.optional_columns_missing,
                "circuit_breaker_tripped": self.circuit_breaker_tripped,
                "shard": None if self.shard is None else asdict(self.shard),
            },
            "summary": self.counts(),
            "flagged_profile": self.flagged_profile,
            "tape_health": self.tape_health.to_dict() if self.tape_health else None,
            "plugins": self.plugins,
            "tape_checks": [t.to_dict() for t in self.tape_checks],
            "tape_findings": [t.to_dict() for t in self.tape_findings],
            "quarantined": [q.to_dict() for q in self.quarantined],
            "loans": [r.to_dict() for r in self.loans],
            **({} if self.shard_facts is None else {"shard_facts": self.shard_facts}),
        }


__all__ = [
    "Axis",
    "Finding",
    "LoanResult",
    "QuarantineRecord",
    "Report",
    "RuleOutcome",
    "ShardInfo",
    "Status",
    "TapeCheck",
    "TapeFinding",
    "verdict_tiers",
]
