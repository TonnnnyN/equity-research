"""Group 3 — downside protection. The most important group for this project: it
reviews stocks that have already fallen, and these three models all answer some
version of "where is the floor?" rather than "what is it worth?"."""
from __future__ import annotations

from equity_research.models import ValuationDerived, ValuationFundamentals
from equity_research.valuation_models._types import GROUP_DOWNSIDE_PROTECTION, ModelResult, STATUS_OK, STATUS_SKIPPED

_DEFAULT_ILLIQUIDITY_HAIRCUT = 0.30


def _skip(model: str, reason: str) -> ModelResult:
    return ModelResult(model=model, group=GROUP_DOWNSIDE_PROTECTION, status=STATUS_SKIPPED, skip_reason=reason)


def _latest_instant(fundamentals: ValuationFundamentals | None, concept: str) -> tuple[float | None, str | None]:
    if fundamentals is None:
        return None, "no ValuationFundamentals available"
    history = fundamentals.concepts.get(concept)
    if history is None or not history.datapoints:
        return None, f"{concept}: no data from SEC companyfacts"
    dp = sorted(history.datapoints, key=lambda p: p.end, reverse=True)[0]
    return dp.value, None


def net_cash_floor(derived: ValuationDerived, fundamentals: ValuationFundamentals | None) -> ModelResult:
    """Per-share liquid assets and Graham net current asset value (NCAV) — "where is
    the floor?". Uses ``total_liquid_assets`` (cash + CURRENT marketable securities),
    deliberately NOT ``cash_and_equivalents`` alone, which understates liquidity by
    up to ~9x for filers that hold current securities outside cash (task spec, Zoom
    example: $0.891B cash vs $7.721B total_liquid_assets)."""
    liquid = derived.total_liquid_assets.value
    shares = derived.shares_used.value
    market_cap = derived.market_cap.value

    if liquid is None or shares is None or shares <= 0:
        return _skip(
            "net_cash_floor",
            f"total_liquid_assets or shares_used unavailable "
            f"({derived.total_liquid_assets.reason or 'ok'}; {derived.shares_used.reason or 'ok'})",
        )

    liquid_per_share = liquid / shares
    current_price = market_cap / shares if market_cap is not None else None

    total_current_assets, ca_reason = _latest_instant(fundamentals, "total_current_assets")
    total_liabilities, tl_reason = _latest_instant(fundamentals, "total_liabilities")

    ncav = None
    ncav_per_share = None
    ncav_reason = None
    if total_current_assets is None or total_liabilities is None:
        ncav_reason = f"NCAV unavailable: {ca_reason or ''} {tl_reason or ''}".strip()
    else:
        ncav = total_current_assets - total_liabilities
        ncav_per_share = ncav / shares

    outputs = {
        "total_liquid_assets": liquid,
        "liquid_assets_per_share": liquid_per_share,
        "current_price": current_price,
        "price_to_liquid_assets_per_share": (current_price / liquid_per_share) if current_price and liquid_per_share else None,
        "ncav": ncav,
        "ncav_per_share": ncav_per_share,
        "price_to_ncav_per_share": (current_price / ncav_per_share) if current_price and ncav_per_share and ncav_per_share > 0 else None,
        "graham_two_thirds_ncav_per_share": (ncav_per_share * 2 / 3) if ncav_per_share and ncav_per_share > 0 else None,
    }

    caveats = [
        "total_liquid_assets (cash + current marketable securities), not cash_and_equivalents alone, is the "
        "correct floor input — see architecture.md's Zoom example.",
        "NCAV = total_current_assets - total_liabilities (classic Graham net-net), not current_assets minus "
        "current_liabilities-only — a stricter, more conservative floor.",
        "This floor is most meaningful for asset-heavy or distressed names; for a cash-rich, low-current-asset "
        "software company it will often show a small or negative NCAV even when the cash floor alone (see "
        "sum_of_the_parts) is substantial — the two floors answer different questions and should both be read.",
    ]
    if ncav_reason:
        caveats.append(ncav_reason)

    return ModelResult(
        model="net_cash_floor",
        group=GROUP_DOWNSIDE_PROTECTION,
        status=STATUS_OK,
        assumptions={},
        outputs=outputs,
        caveats=caveats,
    )


def sum_of_the_parts(
    derived: ValuationDerived,
    illiquidity_haircut: float = _DEFAULT_ILLIQUIDITY_HAIRCUT,
    include_operating_leases_in_net_debt: bool = False,
    operating_lease_liabilities: float | None = None,
) -> ModelResult:
    """Core operating business + net cash + non_operating_assets, each valued
    separately, PLUS the inverse: given the market price, what is the market
    implicitly paying for the core business once cash and strategic stakes are
    stripped out? (task spec — often the single most illuminating number for a
    cash-rich company). The inverse needs no extra assumption beyond the haircut, so
    it is the primary output; a forward "core business" DCF value belongs in
    reverse_dcf / two_stage_dcf and should be cross-checked against this residual.

    ``illiquidity_haircut``: an explicit, parameterised discount applied to
    ``non_operating_assets`` (e.g. Zoom's Anthropic stake) because those positions are
    real value but cannot be realised on demand — default 30%, override to test
    sensitivity.

    ``include_operating_leases_in_net_debt`` / ``operating_lease_liabilities``: the
    open modelling decision from the task spec. Post-ASC 842, most practitioners
    treat operating leases as debt-like, but this data layer's ``total_debt`` /
    ``net_cash`` currently EXCLUDE operating-lease liabilities entirely (see
    architecture.md — Zoom has $60.2M of operating-lease liabilities that are
    invisible to ``total_debt``, and no filer's operating-lease liability is
    currently extracted by ``sources/sec.py`` at all). Default here is
    ``include_operating_leases_in_net_debt=False``, matching the data layer's
    existing (undecided-on-purpose) convention, because the figure is not available
    to default any other way without a silent assumption. Pass
    ``operating_lease_liabilities`` explicitly (e.g. from the 10-K's lease footnote,
    read by the reviewing agent) to include it — immaterial for Zoom, potentially
    material for a lease-heavy filer.
    """
    market_cap = derived.market_cap.value
    net_cash = derived.net_cash.value
    non_op = derived.non_operating_assets.value
    shares = derived.shares_used.value

    if market_cap is None or net_cash is None:
        return _skip(
            "sum_of_the_parts",
            f"market_cap or net_cash unavailable ({derived.market_cap.reason or 'ok'}; {derived.net_cash.reason or 'ok'})",
        )

    adjusted_net_cash = net_cash
    if include_operating_leases_in_net_debt:
        if operating_lease_liabilities is None:
            lease_treatment = (
                "include_operating_leases_in_net_debt=True was requested but operating_lease_liabilities was not "
                "supplied (the data layer does not currently extract this figure for any filer) — treated as 0, "
                "i.e. no adjustment was actually applied; supply the figure explicitly to include it"
            )
        else:
            adjusted_net_cash = net_cash - operating_lease_liabilities
            lease_treatment = f"operating_lease_liabilities={operating_lease_liabilities:,.1f} subtracted from net_cash per include_operating_leases_in_net_debt=True"
    else:
        lease_treatment = (
            "operating lease liabilities EXCLUDED from net debt (default, matches valuation.py's current "
            "total_debt convention). Post-ASC 842 most practitioners treat operating leases as debt-like; set "
            "include_operating_leases_in_net_debt=True with an explicit operating_lease_liabilities figure to "
            "include them. Immaterial for Zoom (~$60.2M) but could be material for a lease-heavy filer."
        )

    non_op_haircut_value = non_op * (1 - illiquidity_haircut) if non_op is not None else None
    implied_core_business_value = market_cap - adjusted_net_cash - (non_op_haircut_value or 0.0)
    per_share_core = implied_core_business_value / shares if shares else None

    outputs = {
        "market_cap": market_cap,
        "net_cash": net_cash,
        "net_cash_after_lease_treatment": adjusted_net_cash,
        "operating_lease_treatment": lease_treatment,
        "non_operating_assets_gross": non_op,
        "illiquidity_haircut": illiquidity_haircut,
        "non_operating_assets_after_haircut": non_op_haircut_value,
        "implied_core_business_value": implied_core_business_value,
        "implied_core_business_value_per_share": per_share_core,
    }

    caveats = [
        "implied_core_business_value is a RESIDUAL (market_cap - net_cash - haircut-adjusted "
        "non_operating_assets), not an independent valuation of the core business — cross-check it against "
        "reverse_dcf / two_stage_dcf's enterprise-value output as a sanity check, not a duplicate.",
        f"illiquidity_haircut={illiquidity_haircut:.0%} is an explicit assumption representing the inability to "
        "realise strategic/venture stakes on demand — vary it to see how much of the conclusion depends on it.",
    ]
    if non_op is None:
        caveats.append("non_operating_assets not reported by this filer (or missing) — SOTP degenerates to a simple net-cash-adjusted market cap; the residual core value may be overstated if illiquid strategic assets exist but were not captured.")

    return ModelResult(
        model="sum_of_the_parts",
        group=GROUP_DOWNSIDE_PROTECTION,
        status=STATUS_OK,
        assumptions={
            "illiquidity_haircut": illiquidity_haircut,
            "include_operating_leases_in_net_debt": include_operating_leases_in_net_debt,
            "operating_lease_liabilities": operating_lease_liabilities,
        },
        outputs=outputs,
        caveats=caveats,
    )


def cash_runway(derived: ValuationDerived) -> ModelResult:
    """Months of liquidity at the current TTM burn rate. Only meaningful when FCF is
    negative — the router must skip this model otherwise, and this function also
    self-guards against being called on a non-negative-FCF profile."""
    fcf = derived.ttm_fcf.value
    liquid = derived.total_liquid_assets.value

    if fcf is None or liquid is None:
        return _skip(
            "cash_runway",
            f"ttm_fcf or total_liquid_assets unavailable ({derived.ttm_fcf.reason or 'ok'}; {derived.total_liquid_assets.reason or 'ok'})",
        )
    if fcf >= 0:
        return _skip("cash_runway", f"ttm_fcf is {fcf:,.1f} (>= 0) — cash runway is only meaningful for a cash-burning company")

    monthly_burn = abs(fcf) / 12
    months = liquid / monthly_burn if monthly_burn > 0 else None

    return ModelResult(
        model="cash_runway",
        group=GROUP_DOWNSIDE_PROTECTION,
        status=STATUS_OK,
        assumptions={"burn_basis": "ttm_fcf / 12 (trailing twelve months, straight-lined)"},
        outputs={
            "ttm_fcf": fcf,
            "total_liquid_assets": liquid,
            "monthly_burn_rate": monthly_burn,
            "runway_months": months,
            "runway_years": (months / 12) if months is not None else None,
        },
        caveats=[
            "Assumes the trailing-twelve-month burn rate persists unchanged going forward — a real early-warning "
            "number, not a forecast of when burn will actually change.",
            "Uses total_liquid_assets (cash + current marketable securities), not cash_and_equivalents alone.",
        ],
    )
