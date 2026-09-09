"""Guarded XIRR (annualised internal rate of return for dated cash flows).

Pure Python bisection on ``xnpv``: no compiled dependency, deterministic, and it returns
``None`` instead of raising when the flows have no root in the search bracket, so the
calling rule can report ``not_evaluable``.
"""

from __future__ import annotations

from datetime import date

CashFlow = tuple[date, float]


def xnpv(rate: float, flows: list[CashFlow]) -> float:
    t0 = flows[0][0]
    npv = 0.0
    for t, cf in flows:
        npv += cf / (1.0 + rate) ** ((t - t0).days / 365.0)
    return npv


def xirr(
    flows: list[CashFlow],
    *,
    low: float = -0.99,
    high: float = 10.0,
    tol: float = 1e-7,
    max_iter: int = 200,
) -> float | None:
    """Annualised rate at which the dated flows net to zero, or None if not solvable."""
    if len(flows) < 2:
        return None
    flows = sorted(flows, key=lambda f: f[0])
    has_out = any(cf < 0 for _, cf in flows)
    has_in = any(cf > 0 for _, cf in flows)
    if not (has_out and has_in):
        return None
    try:
        f_low = xnpv(low, flows)
        f_high = xnpv(high, flows)
    except (OverflowError, ZeroDivisionError):
        return None
    if f_low * f_high > 0:
        return None
    lo, hi = low, high
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        try:
            f_mid = xnpv(mid, flows)
        except (OverflowError, ZeroDivisionError):
            return None
        if abs(f_mid) < tol or (hi - lo) / 2.0 < tol:
            return mid
        if f_low * f_mid < 0:
            hi, f_high = mid, f_mid
        else:
            lo, f_low = mid, f_mid
    return (lo + hi) / 2.0
