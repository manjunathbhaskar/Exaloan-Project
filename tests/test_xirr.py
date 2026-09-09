"""XIRR solver: correctness on known answers, and None (never an exception) when unsolvable."""

from __future__ import annotations

from datetime import date

import pytest

from loan_dq.rules.xirr import xirr, xnpv
from tests.conftest import add_months


def _annuity_flows(principal: float, annual_rate: float, months: int) -> list[tuple[date, float]]:
    r = annual_rate / 12.0
    instalment = principal * r / (1.0 - (1.0 + r) ** -months)
    start = date(2024, 1, 15)
    return [(start, -principal)] + [
        (add_months(start, i), instalment) for i in range(1, months + 1)
    ]


@pytest.mark.parametrize("annual_rate", [0.05, 0.12, 0.25])
def test_xirr_recovers_annuity_rate(annual_rate: float) -> None:
    rate = xirr(_annuity_flows(1000.0, annual_rate, 12))
    assert rate is not None
    # XIRR is an effective annual rate; a nominal r/12 annuity realises (1 + r/12)^12 - 1.
    effective = (1.0 + annual_rate / 12.0) ** 12 - 1.0
    assert abs(rate - effective) < 0.005


def test_xirr_zero_interest_is_zero() -> None:
    flows = [(date(2024, 1, 1), -120.0)] + [
        (add_months(date(2024, 1, 1), i), 10.0) for i in range(1, 13)
    ]
    rate = xirr(flows)
    assert rate is not None
    assert abs(rate) < 1e-4


def test_xirr_single_bullet_repayment_doubling_in_a_year() -> None:
    rate = xirr([(date(2024, 1, 1), -100.0), (date(2025, 1, 1), 200.0)])
    assert rate is not None
    assert abs(rate - 1.0) < 0.01  # 366 days in 2024 -> marginally under 100%


def test_xirr_unsorted_input_is_sorted() -> None:
    flows = [(date(2025, 1, 1), 200.0), (date(2024, 1, 1), -100.0)]
    assert xirr(flows) == xirr(list(reversed(flows)))


@pytest.mark.parametrize(
    "flows",
    [
        [],
        [(date(2024, 1, 1), -100.0)],
        [(date(2024, 1, 1), -100.0), (date(2024, 6, 1), -50.0)],  # no inflow
        [(date(2024, 1, 1), 100.0), (date(2024, 6, 1), 50.0)],  # no outflow
    ],
)
def test_xirr_returns_none_when_not_solvable(flows: list[tuple[date, float]]) -> None:
    assert xirr(flows) is None


def test_xirr_none_when_loss_exceeds_bracket() -> None:
    # Borrower repays 0.1% of principal: rate is below the -99% floor -> not evaluable.
    assert xirr([(date(2024, 1, 1), -1000.0), (date(2025, 1, 1), 1.0)]) is None


def test_xnpv_at_zero_rate_is_plain_sum() -> None:
    flows = [(date(2024, 1, 1), -100.0), (date(2024, 7, 1), 60.0), (date(2025, 1, 1), 60.0)]
    assert xnpv(0.0, flows) == pytest.approx(20.0)
