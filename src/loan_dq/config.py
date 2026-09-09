"""Run configuration: every threshold the rule engine uses lives here, never in rule code.

Values are loaded from a YAML file (see ``config/default.yaml``) and validated by pydantic.
The effective configuration is hashed into the report header so that any verdict can be
reproduced from (input file, config, rule-set version).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

NonnegativeFloat = Annotated[float, Field(ge=0)]
Fraction = Annotated[float, Field(ge=0, le=1)]
NonnegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(ge=1)]
RULE_IDS = frozenset(
    [
        "A2",
        "A3",
        "A4",
        "A5",
        "A6",
        "A7",
        "A8",
        "A9",
        "A10",
        "B1",
        "B2",
        "B3",
        "B4",
        "B5",
        "B6",
        "B7",
        "B11",
        "C1",
        "C2",
        "C4",
        "C5",
        "C6",
        "C8",
        "C9",
        "C10",
        "D1",
        "D2",
        "D3",
        "D4",
        "D5",
        "D6",
        "D7",
        "D8",
        "E1",
        "E2",
        "E3",
        "E4",
        "E5",
        "F1",
        "F2",
        "F3",
        "G6",
        "G7",
        "G8",
        "H1",
        "H3",
        "H4",
        "H5",
        "H9",
        "H10",
    ]
)


class ConfigError(ValueError):
    pass


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


class LatenessConfig(StrictConfig):
    default_days: PositiveInt = 90
    medium_days: PositiveInt = 30
    watch_flag_days: PositiveInt = 60
    grace_days: NonnegativeInt = 5
    habitual_late_share: Fraction = 0.5
    trend_min_paid: Annotated[int, Field(ge=2)] = 6
    drift_recent_episodes: PositiveInt = 3
    drift_min_exceeding: PositiveInt = 2

    @model_validator(mode="after")
    def ordered_thresholds(self) -> LatenessConfig:
        if not self.grace_days < self.medium_days <= self.watch_flag_days < self.default_days:
            raise ValueError("require grace_days < medium_days <= watch_flag_days < default_days")
        if self.drift_min_exceeding > self.drift_recent_episodes:
            raise ValueError("drift_min_exceeding must not exceed drift_recent_episodes")
        return self


class ToleranceConfig(StrictConfig):
    amount_eur: NonnegativeFloat = 0.50
    principal_gap_material_share: Fraction = 0.10
    interest_eur: NonnegativeFloat = 1.00
    days_late_days: NonnegativeInt = 5
    instalment_interest_rel: Fraction = 0.15
    instalment_interest_eur: NonnegativeFloat = 0.50
    schedule_gap_days: PositiveInt = 45
    schedule_min_gap_days: NonnegativeInt = 20
    expected_repayment_days: NonnegativeInt = 45
    repayment_date_slack_days: NonnegativeInt = 3
    grace_state_days: NonnegativeInt = 15
    as_of_isolated_days: NonnegativeInt = 45

    @model_validator(mode="after")
    def ordered_thresholds(self) -> ToleranceConfig:
        if self.schedule_min_gap_days >= self.schedule_gap_days:
            raise ValueError("schedule_min_gap_days must be below schedule_gap_days")
        return self


class XirrConfig(StrictConfig):
    min_inflows: PositiveInt = 3
    min_realised_interest_share: Fraction = 0.10
    gap_percentage_points: NonnegativeFloat = 5.0
    gap_relative: Fraction = 0.40
    low_rate_floor: Annotated[float, Field(gt=-1)] = -0.99
    high_rate_ceiling: Annotated[float, Field(gt=-1)] = 10.0

    @model_validator(mode="after")
    def ordered_thresholds(self) -> XirrConfig:
        if self.low_rate_floor >= self.high_rate_ceiling:
            raise ValueError("low_rate_floor must be below high_rate_ceiling")
        return self


class PlausibilityConfig(StrictConfig):
    min_age: NonnegativeInt = 18
    max_age: PositiveInt = 100
    min_interest_rate: NonnegativeFloat = 0.0
    max_interest_rate: NonnegativeFloat = 100.0
    min_term_months: PositiveInt = 1
    max_term_months: PositiveInt = 360
    max_payment_to_income: NonnegativeFloat = 1.0

    @model_validator(mode="after")
    def ordered_thresholds(self) -> PlausibilityConfig:
        for lower, upper in (
            (self.min_age, self.max_age),
            (self.min_interest_rate, self.max_interest_rate),
            (self.min_term_months, self.max_term_months),
        ):
            if lower > upper:
                raise ValueError("minimum plausibility threshold must not exceed its maximum")
        return self


class TapeConfig(StrictConfig):
    circuit_breaker_share: Fraction = 0.40
    excel_cell_limit: PositiveInt = 32767
    benford_min_sample: PositiveInt = 500
    benford_min_loans: PositiveInt = 200
    benford_max_mad: Fraction = 0.015
    duplicate_amount_floor_eur: NonnegativeFloat = 50.0
    round_min_sample: PositiveInt = 100
    round_share_max: Fraction = 0.10
    round_min_days_late: PositiveInt = 30


class VerdictConfig(StrictConfig):
    """``flag_min_severity``: the lowest root-cause severity that turns a loan's verdict to
    ``flagged``. Findings below it are still reported on the loan as minor observations, but
    the loan stays ``normal`` (the brief defines clean as "no major issues")."""

    flag_min_severity: Literal["info", "low", "medium", "high", "critical"] = "medium"


class SchemaConfig(StrictConfig):
    required_columns: list[str] = Field(
        default_factory=lambda: [
            "Loan ID",
            "Borrower ID",
            "Loan amount",
            "Disbursal date",
            "Interest rate",
            "Loan term",
            "Loan status",
            "payments",
        ]
    )
    optional_columns: list[str] = Field(
        default_factory=lambda: [
            "Expected repayment date",
            "Loan type",
            "Borrower type",
            "Credit score",
            "Monthly payment",
            "Days late",
            "Outstanding principal",
            "Repaid principal",
            "Outstanding interest",
            "Repaid interest",
            "Repayment date",
            "Arrears",
            "Delay interest",
            "Purpose",
            "Birth year",
            "Family income",
            "Borrower income",
            "Family liabilities",
            "Children",
            "Employment status",
            "Company age (years)",
        ]
    )
    loan_status_values: list[str] = Field(
        default_factory=lambda: ["granted", "repaid", "terminated"]
    )
    loan_type_values: list[str] = Field(default_factory=lambda: ["instalment", "deferred annuity"])
    borrower_type_values: list[str] = Field(default_factory=lambda: ["individual", "business"])
    credit_score_values: list[str] = Field(default_factory=lambda: ["A", "B", "C", "D"])
    payment_type_values: list[str] = Field(
        default_factory=lambda: [
            "principal",
            "interest",
            "contract fee repayment",
            "overdue interest",
            "full early repayment",
            "partial early repayment",
            "repayment after agreement termination",
        ]
    )
    payment_state_values: list[str] = Field(
        default_factory=lambda: [
            "paid on time",
            "paid with delay",
            "pending",
            "pending late",
            "payment in grace period",
            "payment pending today",
        ]
    )
    purpose_code_pattern: str = r"^loan_purpose\.\d+$"

    @field_validator("purpose_code_pattern")
    @classmethod
    def valid_pattern(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError("purpose_code_pattern must be a valid regular expression") from exc
        return value

    @model_validator(mode="after")
    def valid_schema(self) -> SchemaConfig:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, list):
                if (name != "optional_columns" and not value) or any(not v.strip() for v in value):
                    raise ValueError(f"{name} must contain nonblank names")
                if len(value) != len(set(value)):
                    raise ValueError(f"{name} must contain unique names")
        if set(self.required_columns) & set(self.optional_columns):
            raise ValueError("required_columns and optional_columns must not overlap")
        return self


class Config(StrictConfig):
    """Top-level configuration. ``as_of`` = None means "infer from the tape"."""

    lender: str = "default"
    as_of: date | None = None
    lateness: LatenessConfig = Field(default_factory=LatenessConfig)
    tolerance: ToleranceConfig = Field(default_factory=ToleranceConfig)
    xirr: XirrConfig = Field(default_factory=XirrConfig)
    plausibility: PlausibilityConfig = Field(default_factory=PlausibilityConfig)
    tape: TapeConfig = Field(default_factory=TapeConfig)
    verdict: VerdictConfig = Field(default_factory=VerdictConfig)
    schema_: SchemaConfig = Field(default_factory=SchemaConfig, alias="schema")
    disabled_rules: list[str] = Field(default_factory=list)
    info_only_rules: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("disabled_rules", "info_only_rules")
    @classmethod
    def valid_rule_ids(cls, values: list[str]) -> list[str]:
        if any(value not in RULE_IDS for value in values):
            raise ValueError("rule IDs must belong to the registered per-loan rule set")
        if len(values) != len(set(values)):
            raise ValueError("rule IDs must be unique")
        return values

    @classmethod
    def load(cls, path: Path | None) -> Config:
        if path is None:
            return cls()
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
            return cls.model_validate({} if raw is None else raw)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in error['loc']) or 'root'}: {error['msg']}"
                for error in exc.errors(include_input=False, include_url=False)[:5]
            )
            raise ConfigError(f"invalid config: {details}") from exc
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ConfigError(f"could not load config ({type(exc).__name__})") from exc

    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json", by_alias=True), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
