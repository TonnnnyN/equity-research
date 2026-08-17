"""The router: a deterministic decision tree (plain Python, no LLM) that picks which
of the 12 models to run from company profile + data availability, and reports which
models were excluded and the specific reason for every one of them.
"""
from __future__ import annotations

from market_sentiment.models import PipelineContext, PriceBar, Security, ValuationDerived, ValuationFundamentals
from market_sentiment.valuation_models._dcf import owner_earnings_valuation, reverse_dcf, two_stage_dcf
from market_sentiment.valuation_models._downside import cash_runway, net_cash_floor, sum_of_the_parts
from market_sentiment.valuation_models._quality import altman_z_score, beneish_m_score, piotroski_f_score
from market_sentiment.valuation_models._relative import own_history_percentile, peer_comparison
from market_sentiment.valuation_models._scenarios import three_scenario_expected_value
from market_sentiment.valuation_models._series import rolling_ttm_series
from market_sentiment.valuation_models._types import (
    GROUP_CASH_FLOW_INTRINSIC,
    GROUP_DOWNSIDE_PROTECTION,
    GROUP_QUALITY_AND_RISK,
    GROUP_RELATIVE_VALUATION,
    ModelResult,
    RouterDecision,
    RouterParams,
    RouterProfile,
    STATUS_SKIPPED,
    ValuationModelReport,
)

MODEL_NAMES: tuple[str, ...] = (
    "reverse_dcf",
    "two_stage_dcf",
    "owner_earnings",
    "own_history_percentile",
    "peer_comparison",
    "net_cash_floor",
    "sum_of_the_parts",
    "cash_runway",
    "piotroski_f_score",
    "altman_z_score",
    "beneish_m_score",
    "three_scenario_expected_value",
)

_GROUP_OF: dict[str, str] = {
    "reverse_dcf": GROUP_CASH_FLOW_INTRINSIC,
    "two_stage_dcf": GROUP_CASH_FLOW_INTRINSIC,
    "owner_earnings": GROUP_CASH_FLOW_INTRINSIC,
    "own_history_percentile": GROUP_RELATIVE_VALUATION,
    "peer_comparison": GROUP_RELATIVE_VALUATION,
    "net_cash_floor": GROUP_DOWNSIDE_PROTECTION,
    "sum_of_the_parts": GROUP_DOWNSIDE_PROTECTION,
    "cash_runway": GROUP_DOWNSIDE_PROTECTION,
    "piotroski_f_score": GROUP_QUALITY_AND_RISK,
    "altman_z_score": GROUP_QUALITY_AND_RISK,
    "beneish_m_score": GROUP_QUALITY_AND_RISK,
    "three_scenario_expected_value": GROUP_QUALITY_AND_RISK,
}

_ALL_MULTIPLES = ("ev_fcf", "ev_sales", "ev_ebitda", "pe")
_FCF_NEGATIVE_MULTIPLES = ("ev_sales",)


def _fcf_series(fundamentals: ValuationFundamentals | None) -> list[tuple]:
    if fundamentals is None:
        return []
    ocf = dict(rolling_ttm_series(fundamentals.concepts.get("operating_cashflow")))
    capex = dict(rolling_ttm_series(fundamentals.concepts.get("capex")))
    dates = sorted(set(ocf) & set(capex), reverse=True)
    return [(d, ocf[d] - abs(capex[d])) for d in dates]


def _select_discount_rates(
    security: Security, beta, params: RouterParams
) -> tuple[tuple[float, ...], str, str]:
    """Never returns a single discount rate — even the CAPM-derived branch expands
    into a +/- range. If beta is unreliable (the common case per architecture.md,
    e.g. Zoom's 0.41 beta at R^2 0.037 on ~125 daily bars), CAPM is not used at all;
    a documented sector-default range is used instead."""
    layer_key = security.layer.value if hasattr(security.layer, "value") else str(security.layer)
    if beta is not None and beta.reliable and beta.beta is not None:
        r_capm = params.risk_free_rate + beta.beta * params.equity_risk_premium
        candidates = sorted({round(r_capm - params.capm_spread, 4), round(r_capm, 4), round(r_capm + params.capm_spread, 4)})
        rates = tuple(r for r in candidates if r > 0)
        if not rates:
            rates = params.sector_default_discount_rates.get(layer_key, params.fallback_discount_rates)
        method = "capm_derived_range"
        note = (
            f"beta={beta.beta:.3f} reliable (R^2={beta.r_squared:.3f}, n={beta.observations}) -> CAPM point "
            f"{r_capm:.4f} expanded to +/-{params.capm_spread} range {rates}; never used as a single point"
        )
    else:
        rates = params.sector_default_discount_rates.get(layer_key, params.fallback_discount_rates)
        method = "sector_default_range"
        reason = beta.reason if beta is not None else "no beta estimate available"
        note = (
            f"beta unreliable or unavailable ({reason}) -> discount rate is NOT derived from CAPM; falling back "
            f"to documented sector-default range for layer={layer_key}: {rates}"
        )
    return rates, method, note


def route(
    security: Security,
    derived: ValuationDerived,
    fundamentals: ValuationFundamentals | None,
    peer_contexts: list[PipelineContext] | None = None,
    params: RouterParams | None = None,
) -> RouterDecision:
    params = params or RouterParams()
    peer_contexts = peer_contexts or []

    fcf = derived.ttm_fcf.value
    if fcf is None:
        fcf_sign = "unknown"
    elif fcf > 0:
        fcf_sign = "positive"
    else:
        fcf_sign = "negative"

    fcf_stable: bool | None = None
    if fcf_sign != "unknown":
        series = _fcf_series(fundamentals)
        if len(series) >= 2:
            last_two = [v for _, v in series[:2]]
            fcf_stable = all(v > 0 for v in last_two) if fcf_sign == "positive" else all(v <= 0 for v in last_two)

    market_cap = derived.market_cap.value
    net_cash = derived.net_cash.value
    non_op = derived.non_operating_assets.value
    net_cash_ratio = (net_cash / market_cap) if net_cash is not None and market_cap not in (None, 0) else None
    non_op_ratio = (non_op / market_cap) if non_op is not None and market_cap not in (None, 0) else None

    discount_rates, discount_method, discount_note = _select_discount_rates(security, derived.beta, params)

    layer_value = security.layer.value if hasattr(security.layer, "value") else str(security.layer)
    profile = RouterProfile(
        fcf_sign=fcf_sign,
        fcf_stable=fcf_stable,
        net_cash_to_market_cap=net_cash_ratio,
        non_operating_to_market_cap=non_op_ratio,
        beta_reliable=bool(derived.beta and derived.beta.reliable),
        discount_rate_method=discount_method,
        discount_rates=discount_rates,
        layer=layer_value,
        notes=[discount_note],
    )

    enabled: list[str] = []
    excluded: list[dict[str, str]] = []

    # ---- Group 1: DCF variants ----
    dcf_models = ("reverse_dcf", "two_stage_dcf", "owner_earnings")
    if fcf_sign == "positive":
        enabled.extend(dcf_models)
        if fcf_stable is False:
            profile.notes.append(
                "FCF positive but NOT stable across the last two TTM windows — DCF models still run, but treat "
                "the growth/value ranges with extra skepticism"
            )
    elif fcf_sign == "negative":
        for name in dcf_models:
            excluded.append(
                {
                    "model": name,
                    "reason": "ttm_fcf is negative — a DCF on a cash-burning company is self-deception; see "
                    "cash_runway, EV/Sales (own_history_percentile / peer_comparison), and altman_z_score instead",
                }
            )
    else:
        for name in dcf_models:
            excluded.append({"model": name, "reason": f"ttm_fcf sign unknown: {derived.ttm_fcf.reason}"})

    # ---- Group 2: relative valuation ----
    multiples = _FCF_NEGATIVE_MULTIPLES if fcf_sign == "negative" else _ALL_MULTIPLES
    enabled.append("own_history_percentile")
    enabled.append("peer_comparison")
    profile.notes.append(
        f"relative-valuation multiples restricted to {multiples} because ttm_fcf is negative"
        if fcf_sign == "negative"
        else f"relative-valuation multiples: {multiples}"
    )

    # ---- Group 3: downside protection ----
    enabled.append("net_cash_floor")

    force_sotp_reasons = []
    if net_cash_ratio is not None and net_cash_ratio > params.net_cash_ratio_force_sotp:
        force_sotp_reasons.append(f"net_cash/market_cap={net_cash_ratio:.1%} > {params.net_cash_ratio_force_sotp:.0%} threshold")
        profile.notes.append("P/E de-emphasised (still reported, flagged): large net_cash relative to market_cap distorts earnings-based multiples")
    if non_op_ratio is not None and non_op_ratio > params.non_operating_assets_materiality:
        force_sotp_reasons.append(f"non_operating_assets/market_cap={non_op_ratio:.1%} > {params.non_operating_assets_materiality:.0%} materiality threshold")
    enabled.append("sum_of_the_parts")
    profile.notes.append(
        "sum_of_the_parts FORCED by: " + "; ".join(force_sotp_reasons)
        if force_sotp_reasons
        else "sum_of_the_parts run by default (net-cash and non-operating-asset ratios below force thresholds)"
    )

    if fcf_sign == "negative":
        enabled.append("cash_runway")
    else:
        excluded.append(
            {
                "model": "cash_runway",
                "reason": f"ttm_fcf is {fcf_sign} — cash runway is only meaningful when FCF is negative",
            }
        )

    # ---- Group 4: quality and risk — always attempted; each model self-skips on its own data gaps ----
    enabled.extend(["piotroski_f_score", "altman_z_score", "beneish_m_score", "three_scenario_expected_value"])

    excluded_names = {e["model"] for e in excluded}
    enabled = [m for m in enabled if m not in excluded_names]

    return RouterDecision(enabled_models=enabled, excluded=excluded, profile=profile)


def _run_model(
    name: str,
    security: Security,
    derived: ValuationDerived,
    fundamentals: ValuationFundamentals | None,
    prices: list[PriceBar],
    peer_contexts: list[PipelineContext],
    decision: RouterDecision,
) -> ModelResult:
    rates = decision.profile.discount_rates
    multiples = _FCF_NEGATIVE_MULTIPLES if decision.profile.fcf_sign == "negative" else _ALL_MULTIPLES

    if name == "reverse_dcf":
        return reverse_dcf(derived, discount_rates=rates)
    if name == "two_stage_dcf":
        return two_stage_dcf(derived, discount_rates=rates)
    if name == "owner_earnings":
        return owner_earnings_valuation(derived, discount_rates=rates)
    if name == "own_history_percentile":
        return own_history_percentile(fundamentals, derived, prices, multiples=multiples)
    if name == "peer_comparison":
        return peer_comparison(derived, security, peer_contexts, multiples=multiples)
    if name == "net_cash_floor":
        return net_cash_floor(derived, fundamentals)
    if name == "sum_of_the_parts":
        return sum_of_the_parts(derived)
    if name == "cash_runway":
        return cash_runway(derived)
    if name == "piotroski_f_score":
        return piotroski_f_score(fundamentals)
    if name == "altman_z_score":
        return altman_z_score(derived, fundamentals, layer=security.layer)
    if name == "beneish_m_score":
        return beneish_m_score(fundamentals)
    if name == "three_scenario_expected_value":
        return three_scenario_expected_value(derived)
    raise AssertionError(f"unknown model name: {name}")  # pragma: no cover — MODEL_NAMES is the only caller input


def evaluate(
    security: Security,
    derived: ValuationDerived,
    fundamentals: ValuationFundamentals | None,
    prices: list[PriceBar],
    peer_contexts: list[PipelineContext] | None = None,
    params: RouterParams | None = None,
) -> ValuationModelReport:
    """Run the router, then every enabled model, and assemble the full report. Every
    one of the 12 models appears exactly once in ``results`` — either its real
    output or a structured skip with a reason, whether the skip came from the
    router (profile-driven) or from the model itself (data-driven)."""
    peer_contexts = peer_contexts or []
    decision = route(security, derived, fundamentals, peer_contexts, params)

    excluded_reason = {e["model"]: e["reason"] for e in decision.excluded}
    results: list[ModelResult] = []
    for name in MODEL_NAMES:
        if name in excluded_reason:
            results.append(
                ModelResult(
                    model=name,
                    group=_GROUP_OF[name],
                    status=STATUS_SKIPPED,
                    skip_reason=f"[router] {excluded_reason[name]}",
                )
            )
            continue
        results.append(_run_model(name, security, derived, fundamentals, prices, peer_contexts, decision))

    return ValuationModelReport(ticker=security.ticker, as_of=derived.as_of, router=decision, results=results)
