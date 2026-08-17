"""Group 2 — relative valuation: own-history percentile, peer comparison."""
from __future__ import annotations

import statistics
from datetime import date

from equity_research.models import PipelineContext, PriceBar, Security, ValuationDerived, ValuationFundamentals
from equity_research.valuation_models._series import instant_series, nearest_price, rolling_ttm_series
from equity_research.valuation_models._types import GROUP_RELATIVE_VALUATION, ModelResult, STATUS_OK, STATUS_SKIPPED

_ALL_MULTIPLES = ("ev_fcf", "ev_sales", "ev_ebitda", "pe")
_DEFAULT_MIN_RELIABLE_OBSERVATIONS = 8
_PRICE_MATCH_TOLERANCE_DAYS = 7


def _skip(model: str, reason: str) -> ModelResult:
    return ModelResult(model=model, group=GROUP_RELATIVE_VALUATION, status=STATUS_SKIPPED, skip_reason=reason)


def _current_multiples(derived: ValuationDerived) -> dict[str, float | None]:
    ev = derived.enterprise_value.value
    mc = derived.market_cap.value
    fcf = derived.ttm_fcf.value
    sales = derived.ttm_revenue.value
    ebitda = derived.ttm_ebitda.value
    net_income = derived.ttm_net_income.value

    out: dict[str, float | None] = {"ev_fcf": None, "ev_sales": None, "ev_ebitda": None, "pe": None}
    if ev is not None:
        if fcf and fcf > 0:
            out["ev_fcf"] = ev / fcf
        if sales and sales > 0:
            out["ev_sales"] = ev / sales
        if ebitda and ebitda > 0:
            out["ev_ebitda"] = ev / ebitda
    if mc is not None and net_income and net_income > 0:
        out["pe"] = mc / net_income
    return out


def _historical_multiples(
    fundamentals: ValuationFundamentals, prices: list[PriceBar]
) -> dict[str, list[tuple[date, float]]]:
    """Reconstruct a quarter-by-quarter multiple series from the SEC concept history
    (up to 20 quarters) matched against the nearest available price bar per
    quarter-end. A quarter with no price bar within tolerance, or with a missing
    required concept, is silently excluded from that metric's series — never
    approximated — which is exactly why the series is usually short (see caveats)."""
    revenue = dict(rolling_ttm_series(fundamentals.concepts.get("revenue")))
    op_income = dict(rolling_ttm_series(fundamentals.concepts.get("operating_income")))
    da = dict(rolling_ttm_series(fundamentals.concepts.get("depreciation_amortization")))
    ocf = dict(rolling_ttm_series(fundamentals.concepts.get("operating_cashflow")))
    capex = dict(rolling_ttm_series(fundamentals.concepts.get("capex")))
    net_income = dict(rolling_ttm_series(fundamentals.concepts.get("net_income")))
    cash = dict(instant_series(fundamentals.concepts.get("cash_and_equivalents")))
    sti = dict(instant_series(fundamentals.concepts.get("short_term_investments")))
    debt = dict(instant_series(fundamentals.concepts.get("long_term_debt")))
    lease = dict(instant_series(fundamentals.concepts.get("finance_lease_obligations")))
    shares = dict(
        instant_series(fundamentals.concepts.get("diluted_weighted_avg_shares"))
        or instant_series(fundamentals.concepts.get("basic_weighted_avg_shares"))
    )

    series: dict[str, list[tuple[date, float]]] = {name: [] for name in _ALL_MULTIPLES}
    for end_date in sorted(revenue, reverse=True):
        price = nearest_price(prices, end_date, max_gap_days=_PRICE_MATCH_TOLERANCE_DAYS)
        share_count = shares.get(end_date)
        if price is None or not share_count or share_count <= 0:
            continue
        market_cap = price * share_count

        liquid = cash.get(end_date)
        if liquid is None:
            continue
        liquid += sti.get(end_date, 0.0)
        total_debt = debt.get(end_date, 0.0) + lease.get(end_date, 0.0)
        ev = market_cap - liquid + total_debt

        rev = revenue.get(end_date)
        if rev and rev > 0:
            series["ev_sales"].append((end_date, ev / rev))

        if end_date in ocf and end_date in capex:
            fcf = ocf[end_date] - abs(capex[end_date])
            if fcf > 0:
                series["ev_fcf"].append((end_date, ev / fcf))

        if end_date in op_income and end_date in da:
            ebitda = op_income[end_date] + da[end_date]
            if ebitda > 0:
                series["ev_ebitda"].append((end_date, ev / ebitda))

        ni = net_income.get(end_date)
        if ni and ni > 0:
            series["pe"].append((end_date, market_cap / ni))

    return series


def own_history_percentile(
    fundamentals: ValuationFundamentals | None,
    derived: ValuationDerived,
    prices: list[PriceBar],
    multiples: tuple[str, ...] = _ALL_MULTIPLES,
    min_reliable_observations: int = _DEFAULT_MIN_RELIABLE_OBSERVATIONS,
) -> ModelResult:
    """Where today's EV/FCF, EV/Sales, EV/EBITDA, P/E sit within this company's OWN
    history, built as a real time series from up to 20 quarters of concept history
    matched to the nearest daily price bar per quarter-end (task spec). Always states
    the observation count — with few quarters a percentile is noise, and this says so
    explicitly rather than presenting a percentile as if it were reliable."""
    if fundamentals is None:
        return _skip("own_history_percentile", "no ValuationFundamentals available")

    historical = _historical_multiples(fundamentals, prices)
    current = _current_multiples(derived)

    per_metric: dict[str, dict] = {}
    any_ok = False
    for metric in multiples:
        cur = current.get(metric)
        series = historical.get(metric, [])
        if cur is None:
            per_metric[metric] = {"status": STATUS_SKIPPED, "reason": f"current {metric} not computable (missing or non-positive input)"}
            continue
        if not series:
            per_metric[metric] = {
                "status": STATUS_SKIPPED,
                "reason": "no historical observation reconstructable — no quarter-end had both the required "
                "fundamentals and a price bar within tolerance (own-history is bounded by price-history depth, "
                "typically ~6 months in this pipeline, far short of 20 quarters)",
                "current_value": cur,
            }
            continue
        values = [v for _, v in series]
        n = len(values)
        rank = sum(1 for v in values if v <= cur)
        percentile = 100.0 * rank / n
        entry = {
            "status": STATUS_OK,
            "current_value": cur,
            "observations": n,
            "percentile": percentile,
            "history_span": [series[-1][0].isoformat(), series[0][0].isoformat()],
        }
        if n < min_reliable_observations:
            entry["reliability_note"] = (
                f"only {n} own-history observation(s) (need >= {min_reliable_observations} to treat the "
                "percentile as more than directional) — this percentile is noise, not a reliable read"
            )
        per_metric[metric] = entry
        any_ok = True

    if not any_ok:
        return _skip(
            "own_history_percentile",
            "no requested multiple had both a current value and any reconstructable own-history observation",
        )

    return ModelResult(
        model="own_history_percentile",
        group=GROUP_RELATIVE_VALUATION,
        status=STATUS_OK,
        assumptions={
            "multiples_requested": list(multiples),
            "min_reliable_observations": min_reliable_observations,
            "price_match_tolerance_days": _PRICE_MATCH_TOLERANCE_DAYS,
        },
        outputs={"multiples": per_metric},
        caveats=[
            "Observation count is capped by DAILY PRICE HISTORY depth (~6 months / ~2 quarters typically per "
            "architecture.md), not by the 20-quarter concept history — check `observations` per metric before "
            "treating any percentile here as meaningful; with 2-3 observations it usually is not.",
        ],
    )


def peer_comparison(
    derived: ValuationDerived,
    security: Security,
    peer_contexts: list[PipelineContext],
    multiples: tuple[str, ...] = _ALL_MULTIPLES,
) -> ModelResult:
    """Compare the subject's current multiples against same-`Security.layer` peers
    already carried on `PipelineContext.peer_contexts` (via the pipeline's
    layer_peers grouping). ALWAYS warns that a cheap multiple inside an expensive
    sector means nothing — this is not a conditional caveat."""
    subject = _current_multiples(derived)

    peers: list[dict] = []
    excluded_peers: list[dict] = []
    for ctx in peer_contexts:
        if ctx.security.ticker == security.ticker:
            continue
        if ctx.security.layer != security.layer:
            continue
        peer_derived = getattr(ctx, "valuation_derived", None)
        if peer_derived is None:
            excluded_peers.append({"ticker": ctx.security.ticker, "reason": "no valuation_derived on this peer's PipelineContext"})
            continue
        peers.append({"ticker": ctx.security.ticker, "multiples": _current_multiples(peer_derived)})

    if not peers:
        return _skip(
            "peer_comparison",
            f"no usable same-layer ({security.layer.value}) peer with valuation_derived data "
            f"(peer_contexts had {len(peer_contexts)} entries, {len(excluded_peers)} excluded)",
        )

    per_metric: dict[str, dict] = {}
    for metric in multiples:
        peer_values = [(p["ticker"], p["multiples"].get(metric)) for p in peers]
        usable = [(t, v) for t, v in peer_values if v is not None]
        entry: dict = {
            "peer_tickers_used": [t for t, _ in usable],
            "peer_count": len(usable),
            "subject_value": subject.get(metric),
        }
        if usable:
            values = [v for _, v in usable]
            entry["peer_median"] = statistics.median(values)
            entry["peer_mean"] = statistics.fmean(values)
            entry["peer_values"] = {t: v for t, v in usable}
            if entry["subject_value"] is not None and entry["peer_median"]:
                entry["subject_vs_peer_median_pct"] = (entry["subject_value"] / entry["peer_median"] - 1) * 100
        per_metric[metric] = entry

    return ModelResult(
        model="peer_comparison",
        group=GROUP_RELATIVE_VALUATION,
        status=STATUS_OK,
        assumptions={"multiples_requested": list(multiples), "layer": security.layer.value},
        outputs={
            "multiples": per_metric,
            "peer_set": [p["ticker"] for p in peers],
            "peers_excluded": excluded_peers,
        },
        caveats=[
            "A cheap multiple inside an expensive sector means nothing — this comparison is relative to "
            f"{len(peers)} same-layer peer(s) only, not to the market or to an absolute standard. Cross-check "
            "against own_history_percentile and, ideally, a peer set outside this project's layer taxonomy.",
            f"peer set used: {[p['ticker'] for p in peers]}"
            + (f"; excluded: {excluded_peers}" if excluded_peers else ""),
        ],
    )
