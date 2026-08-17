"""Shared time-series reconstruction helpers used by the relative-valuation and
quality/risk models. Nothing here is a model — pure data reshaping over the
``ConceptHistory`` / ``PriceBar`` objects the data layer already produces.

All of this is possible only because ``sources/sec.py`` keeps up to 20 quarters of
history per concept (see architecture.md § Valuation Data Layer), not just the
latest datapoint. The 20-quarter depth is what lets own-history percentile and
Piotroski's year-over-year deltas exist at all; the daily price-bar depth
(~6 months per architecture.md) is the binding constraint in practice — see the
caveats each caller attaches.
"""
from __future__ import annotations

from datetime import date

from equity_research.models import ConceptHistory, PriceBar

_MAX_TTM_GAP_DAYS = 100  # matches valuation.py's _MAX_CONTIGUOUS_QUARTER_GAP_DAYS
_DEFAULT_PRICE_TOLERANCE_DAYS = 7
_PRIOR_YEAR_MIN_GAP_DAYS = 350
_PRIOR_YEAR_MAX_GAP_DAYS = 380


def rolling_ttm_series(
    history: ConceptHistory | None, max_gap_days: int = _MAX_TTM_GAP_DAYS
) -> list[tuple[date, float]]:
    """Every trailing-twelve-month sum obtainable by sliding a 4-quarter window over
    ``history.datapoints``, most-recent-first. Each window is required to be
    contiguous (max gap between consecutive quarter-ends <= ``max_gap_days``,
    mirroring valuation.py's single-TTM contiguity check) — a non-contiguous window
    is dropped rather than summed, so the series never contains a plausible-looking
    but wrong TTM figure.
    """
    if history is None or not history.datapoints:
        return []
    points = sorted(history.datapoints, key=lambda p: p.end, reverse=True)
    series: list[tuple[date, float]] = []
    for i in range(len(points) - 3):
        window = points[i : i + 4]
        max_gap = max((window[j].end - window[j + 1].end).days for j in range(3))
        if max_gap > max_gap_days:
            continue
        series.append((window[0].end, sum(p.value for p in window)))
    return series


def instant_series(history: ConceptHistory | None) -> list[tuple[date, float]]:
    """Every reported instant (balance-sheet) datapoint, most-recent-first."""
    if history is None or not history.datapoints:
        return []
    return sorted(((p.end, p.value) for p in history.datapoints), key=lambda t: t[0], reverse=True)


def nearest_price(
    prices: list[PriceBar], target: date, max_gap_days: int = _DEFAULT_PRICE_TOLERANCE_DAYS
) -> float | None:
    """Closest trading-day close to ``target`` within ``max_gap_days`` (either
    direction — a quarter-end is often a weekend). Returns None, never a stale
    fabricated price, when no bar is within tolerance."""
    best: float | None = None
    best_gap: int | None = None
    for bar in prices:
        if not bar.close:
            continue
        gap = abs((bar.trading_date - target).days)
        if gap <= max_gap_days and (best_gap is None or gap < best_gap):
            best, best_gap = bar.close, gap
    return best


def prior_year_pair(
    series: list[tuple[date, float]],
    min_gap_days: int = _PRIOR_YEAR_MIN_GAP_DAYS,
    max_gap_days: int = _PRIOR_YEAR_MAX_GAP_DAYS,
) -> tuple[float | None, float | None]:
    """(current, ~1-year-ago) pair from a most-recent-first series. The prior value
    is only accepted if its date is 350-380 days before the current one — a loose
    band around 365 days that tolerates normal fiscal-calendar drift without pairing
    two unrelated periods. Returns (current, None) if no matching prior exists, and
    (None, None) if the series itself is empty."""
    if not series:
        return None, None
    current_date, current_value = series[0]
    for d, v in series[1:]:
        gap = (current_date - d).days
        if min_gap_days <= gap <= max_gap_days:
            return current_value, v
    return current_value, None
