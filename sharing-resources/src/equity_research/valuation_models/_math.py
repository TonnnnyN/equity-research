"""Pure numeric helpers for the DCF-family models. Stdlib only (bisection, no
scipy) per the project's no-new-dependencies constraint."""
from __future__ import annotations

from typing import Callable

_BISECTION_TOLERANCE = 1e-6
_BISECTION_MAX_ITER = 200


def bisect(f: Callable[[float], float], lo: float, hi: float) -> float | None:
    """Standard bisection root-find on [lo, hi]. Returns None if f does not change
    sign across the bracket (no root in range, or f is not monotonic there) — the
    caller must treat that as "could not solve," never guess an answer."""
    f_lo, f_hi = f(lo), f(hi)
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if f_lo * f_hi > 0:
        return None
    for _ in range(_BISECTION_MAX_ITER):
        mid = (lo + hi) / 2
        f_mid = f(mid)
        if abs(f_mid) < _BISECTION_TOLERANCE or (hi - lo) / 2 < _BISECTION_TOLERANCE:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def pv_growing_fcf_with_terminal_value(
    fcf0: float, growth_rate: float, discount_rate: float, forecast_years: int, terminal_growth: float
) -> float | None:
    """Present value of FCF growing at ``growth_rate`` for ``forecast_years``, plus a
    Gordon-growth terminal value at ``terminal_growth`` thereafter, discounted at
    ``discount_rate``. Returns None when discount_rate <= terminal_growth (the
    terminal-value denominator would be non-positive — an invalid combination, not a
    number to paper over)."""
    if discount_rate <= terminal_growth:
        return None
    total = 0.0
    for t in range(1, forecast_years + 1):
        fcf_t = fcf0 * (1 + growth_rate) ** t
        total += fcf_t / (1 + discount_rate) ** t
    terminal_fcf = fcf0 * (1 + growth_rate) ** forecast_years * (1 + terminal_growth)
    terminal_value = terminal_fcf / (discount_rate - terminal_growth)
    total += terminal_value / (1 + discount_rate) ** forecast_years
    return total


def gordon_growth_value(fcf0: float, growth_rate: float, discount_rate: float) -> float | None:
    """Single-stage Gordon-growth perpetuity value. None when discount_rate <=
    growth_rate (undefined)."""
    if discount_rate <= growth_rate:
        return None
    return fcf0 * (1 + growth_rate) / (discount_rate - growth_rate)
