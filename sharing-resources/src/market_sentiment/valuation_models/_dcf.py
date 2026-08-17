"""Group 1 — cash-flow intrinsic value: reverse DCF, two-stage DCF, owner earnings.

Governing principle (see module docstring in __init__.py): Python computes, Python
does not assume. Every assumption here is a parameter with a documented default;
every output is a range or a grid, never a single point estimate.
"""
from __future__ import annotations

from market_sentiment.models import ValuationDerived
from market_sentiment.valuation_models._math import (
    gordon_growth_value,
    pv_growing_fcf_with_terminal_value,
)
from market_sentiment.valuation_models._math import bisect
from market_sentiment.valuation_models._types import GROUP_CASH_FLOW_INTRINSIC, ModelResult, STATUS_OK, STATUS_SKIPPED

_DEFAULT_DISCOUNT_RATES = (0.08, 0.10, 0.12)
_DEFAULT_GROWTH_BOUNDS = (-0.50, 1.00)  # implied-growth search bracket for reverse DCF


def _skip(model: str, reason: str) -> ModelResult:
    return ModelResult(model=model, group=GROUP_CASH_FLOW_INTRINSIC, status=STATUS_SKIPPED, skip_reason=reason)


def reverse_dcf(
    derived: ValuationDerived,
    forecast_years: int = 10,
    discount_rates: tuple[float, ...] = _DEFAULT_DISCOUNT_RATES,
    terminal_growth: float = 0.025,
    fcf_basis: str = "ttm_fcf",
    growth_search_bounds: tuple[float, float] = _DEFAULT_GROWTH_BOUNDS,
) -> ModelResult:
    """The default model for this project. Takes the current enterprise value as
    given and solves (bisection, stdlib only) for the FCF growth rate the market is
    implicitly assuming over ``forecast_years``, at each of ``discount_rates`` — never
    a single number, because the answer is a function of the discount-rate
    assumption. Rationale (task spec): this project reviews pullbacks, where "how
    pessimistic has the market become, and is that pessimism justified?" is a
    judgement the reading agent can make; forecasting a growth rate from nothing is
    not.

    ``fcf_basis`` selects which ``ValuationDerived`` field is the FCF proxy
    (``ttm_fcf`` or ``ttm_fcf_ex_sbc``); note that this project's ``ttm_fcf`` (OCF -
    |capex|) is a levered-cash-flow proxy, not a textbook unlevered FCFF — the target
    solved against is ``enterprise_value`` (market_cap - net_cash) for consistency
    with the rest of the data layer, which is itself a simplification stated here
    explicitly rather than buried.
    """
    fcf_field = getattr(derived, fcf_basis)
    if fcf_field.value is None:
        return _skip("reverse_dcf", f"{fcf_basis} unavailable: {fcf_field.reason or 'no data'}")
    if fcf_field.value <= 0:
        return _skip(
            "reverse_dcf",
            f"{fcf_basis} is {fcf_field.value:,.1f} (<= 0) — a reverse DCF requires positive current FCF; "
            "see cash_runway / EV-Sales models instead",
        )
    ev_field = derived.enterprise_value
    if ev_field.value is None:
        return _skip("reverse_dcf", f"enterprise_value unavailable: {ev_field.reason or 'no data'}")
    if ev_field.value <= 0:
        return _skip(
            "reverse_dcf",
            f"enterprise_value is {ev_field.value:,.1f} (<= 0, net cash exceeds market cap) — reverse DCF is "
            "undefined here; see net_cash_floor / sum_of_the_parts instead",
        )

    by_rate: list[dict] = []
    for rate in discount_rates:
        if rate <= terminal_growth:
            by_rate.append(
                {
                    "discount_rate": rate,
                    "implied_fcf_growth": None,
                    "note": f"discount_rate ({rate}) <= terminal_growth ({terminal_growth}) — invalid combination, skipped",
                }
            )
            continue

        def f(g: float, _rate: float = rate) -> float:
            pv = pv_growing_fcf_with_terminal_value(fcf_field.value, g, _rate, forecast_years, terminal_growth)
            return (pv if pv is not None else float("inf")) - ev_field.value

        lo, hi = growth_search_bounds
        implied_growth = bisect(f, lo, hi)
        entry = {"discount_rate": rate, "implied_fcf_growth": implied_growth}
        if implied_growth is None:
            entry["note"] = (
                f"no root found in growth search bounds {growth_search_bounds} — the market-implied growth is "
                "outside this bracket (either priced for extreme growth or extreme decline); widen "
                "growth_search_bounds to locate it"
            )
        by_rate.append(entry)

    return ModelResult(
        model="reverse_dcf",
        group=GROUP_CASH_FLOW_INTRINSIC,
        status=STATUS_OK,
        assumptions={
            "forecast_years": forecast_years,
            "discount_rates": list(discount_rates),
            "terminal_growth": terminal_growth,
            "fcf_basis": fcf_basis,
            "growth_search_bounds": list(growth_search_bounds),
        },
        outputs={
            "fcf0": fcf_field.value,
            "enterprise_value_target": ev_field.value,
            "implied_growth_by_discount_rate": by_rate,
        },
        caveats=[
            "This is the growth rate the market is implicitly pricing in, solved from the CURRENT enterprise "
            "value — it is not a forecast and not a fair-value target. Read it as: 'the market is behaving as "
            "if FCF grows at g% for N years' and judge whether that g is pessimistic, realistic, or optimistic "
            "given what you know about the company.",
            "Reported across multiple discount rates deliberately — the implied growth is sensitive to this "
            "assumption and there is no single 'right' discount rate here (see the router's discount_rate_method).",
        ],
    )


def two_stage_dcf(
    derived: ValuationDerived,
    stage1_years: int = 5,
    stage1_growth_rates: tuple[float, ...] = (0.00, 0.05, 0.10, 0.15, 0.20),
    terminal_growth: float = 0.025,
    discount_rates: tuple[float, ...] = _DEFAULT_DISCOUNT_RATES,
    fcf_basis: str = "ttm_fcf",
) -> ModelResult:
    """Stage-1 growth for ``stage1_years``, then ``terminal_growth`` in perpetuity,
    discounted at each rate in ``discount_rates``. Returns a full grid of
    (enterprise value, implied per-share value) across growth x discount-rate cells
    — never a single 'fair value', per the task's governing principle."""
    fcf_field = getattr(derived, fcf_basis)
    if fcf_field.value is None:
        return _skip("two_stage_dcf", f"{fcf_basis} unavailable: {fcf_field.reason or 'no data'}")
    if fcf_field.value <= 0:
        return _skip(
            "two_stage_dcf",
            f"{fcf_basis} is {fcf_field.value:,.1f} (<= 0) — a DCF on negative current FCF is self-deception; "
            "see cash_runway / EV-Sales instead",
        )

    net_cash = derived.net_cash.value
    shares = derived.shares_used.value

    grid: list[dict] = []
    for rate in discount_rates:
        row = {"discount_rate": rate, "cells": []}
        for growth in stage1_growth_rates:
            ev = pv_growing_fcf_with_terminal_value(fcf_field.value, growth, rate, stage1_years, terminal_growth)
            cell = {"stage1_growth": growth, "enterprise_value": ev}
            if ev is None:
                cell["note"] = f"discount_rate ({rate}) <= terminal_growth ({terminal_growth}) — invalid"
            else:
                if net_cash is not None and shares is not None and shares > 0:
                    equity_value = ev + net_cash
                    cell["implied_price_per_share"] = equity_value / shares
                else:
                    cell["implied_price_per_share"] = None
                    cell["note"] = "net_cash or shares_used unavailable — enterprise_value only"
            row["cells"].append(cell)
        grid.append(row)

    caveats = [
        "Grid, not a point: read the whole surface. A wide spread across growth/discount-rate cells IS the "
        "finding — it means the 'fair value' is highly assumption-dependent, which is the honest answer for a "
        "recently-pulled-back stock.",
    ]
    if net_cash is None or shares is None:
        caveats.append("implied_price_per_share could not be computed for any cell (net_cash or shares_used missing) — enterprise_value cells are still valid.")

    return ModelResult(
        model="two_stage_dcf",
        group=GROUP_CASH_FLOW_INTRINSIC,
        status=STATUS_OK,
        assumptions={
            "stage1_years": stage1_years,
            "stage1_growth_rates": list(stage1_growth_rates),
            "terminal_growth": terminal_growth,
            "discount_rates": list(discount_rates),
            "fcf_basis": fcf_basis,
        },
        outputs={"fcf0": fcf_field.value, "net_cash": net_cash, "shares_used": shares, "grid": grid},
        caveats=caveats,
    )


def owner_earnings_valuation(
    derived: ValuationDerived,
    growth_rates: tuple[float, ...] = (0.02, 0.04, 0.06),
    discount_rates: tuple[float, ...] = _DEFAULT_DISCOUNT_RATES,
) -> ModelResult:
    """FCF minus stock-based compensation. SBC is a real cost to shareholders even
    though it consumes no cash — buybacks that merely offset dilution are the cash
    proof of that. Reports BOTH the headline-FCF (``ttm_fcf``) and the SBC-adjusted
    (``ttm_fcf_ex_sbc``) single-stage Gordon-growth valuation grid side by side so the
    gap is visible; for many software names this gap changes the conclusion by ~2x
    (task spec)."""
    headline = derived.ttm_fcf
    adjusted = derived.ttm_fcf_ex_sbc
    net_cash = derived.net_cash.value
    shares = derived.shares_used.value

    if headline.value is None and adjusted.value is None:
        return _skip(
            "owner_earnings",
            f"neither ttm_fcf nor ttm_fcf_ex_sbc available: {headline.reason or ''} / {adjusted.reason or ''}".strip(" /"),
        )

    def grid_for(fcf0: float | None) -> tuple[list[dict], str | None]:
        if fcf0 is None:
            return [], "fcf basis unavailable"
        if fcf0 <= 0:
            return [], f"fcf0 is {fcf0:,.1f} (<= 0) — Gordon-growth valuation is undefined for negative/zero FCF"
        rows = []
        for rate in discount_rates:
            row = {"discount_rate": rate, "cells": []}
            for growth in growth_rates:
                value = gordon_growth_value(fcf0, growth, rate)
                cell = {"growth_rate": growth, "enterprise_value": value}
                if value is not None and net_cash is not None and shares:
                    cell["implied_price_per_share"] = (value + net_cash) / shares
                else:
                    cell["implied_price_per_share"] = None
                row["cells"].append(cell)
            rows.append(row)
        return rows, None

    headline_grid, headline_note = grid_for(headline.value)
    adjusted_grid, adjusted_note = grid_for(adjusted.value)

    outputs = {
        "headline_fcf": headline.value,
        "sbc_adjusted_fcf": adjusted.value,
        "ttm_sbc": derived.ttm_sbc.value,
        "headline_valuation_grid": headline_grid,
        "headline_grid_note": headline_note,
        "sbc_adjusted_valuation_grid": adjusted_grid,
        "sbc_adjusted_grid_note": adjusted_note,
    }
    if headline.value and adjusted.value is not None and headline.value != 0:
        outputs["sbc_drag_ratio"] = adjusted.value / headline.value
        outputs["sbc_drag_pct_of_headline_fcf"] = (1 - adjusted.value / headline.value) * 100

    caveats = [
        "Compare the two grids at matching cells: a large gap between the headline and SBC-adjusted implied "
        "price is the finding, not either grid alone. Do not report only the headline-FCF valuation.",
        "SBC is added back to cash flow statements as a non-cash expense, but it dilutes existing shareholders "
        "every period — buybacks that merely offset that dilution are the cash proof it is a real economic cost, "
        "not merely an accounting one.",
    ]
    if headline_note:
        caveats.append(f"headline grid unavailable: {headline_note}")
    if adjusted_note:
        caveats.append(f"sbc-adjusted grid unavailable: {adjusted_note}")

    return ModelResult(
        model="owner_earnings",
        group=GROUP_CASH_FLOW_INTRINSIC,
        status=STATUS_OK,
        assumptions={"growth_rates": list(growth_rates), "discount_rates": list(discount_rates)},
        outputs=outputs,
        caveats=caveats,
    )
