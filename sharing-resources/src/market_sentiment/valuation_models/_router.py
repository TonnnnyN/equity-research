"""run_order: The AI agent chooses which models to run; Python runs them.

This module no longer contains routing logic (that moved to the caller). Instead,
it defines MODEL_NAMES and the single entry point run_order(), which runs exactly
the models the caller orders, with exactly the assumptions the caller supplies.

The four always-on metrics (piotroski_f_score, altman_z_score, beneish_m_score,
net_cash_floor) compute on every call regardless of whether the order names them,
because they are pure formulas — facts, like the share price.
"""
from __future__ import annotations

from market_sentiment.models import PipelineContext, PriceBar, Security, ValuationDerived, ValuationFundamentals
from market_sentiment.valuation_models._dcf import owner_earnings_valuation, reverse_dcf, two_stage_dcf
from market_sentiment.valuation_models._downside import cash_runway, net_cash_floor, sum_of_the_parts
from market_sentiment.valuation_models._quality import altman_z_score, beneish_m_score, piotroski_f_score
from market_sentiment.valuation_models._relative import own_history_percentile, peer_comparison
from market_sentiment.valuation_models._scenarios import three_scenario_expected_value
from market_sentiment.valuation_models._types import (
    GROUP_CASH_FLOW_INTRINSIC,
    GROUP_DOWNSIDE_PROTECTION,
    GROUP_QUALITY_AND_RISK,
    GROUP_RELATIVE_VALUATION,
    ModelOrder,
    ModelResult,
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

# The four metrics that always compute, regardless of whether the order names them.
# These are pure formulas, not assumption-sensitive. Reason: they are facts, like
# the share price, and must not be quietly omitted on a bullish day.
_ALWAYS_ON_MODELS = frozenset(("piotroski_f_score", "altman_z_score", "beneish_m_score", "net_cash_floor"))


def run_order(
    security: Security,
    order: ModelOrder,
    derived: ValuationDerived,
    fundamentals: ValuationFundamentals | None,
    prices: list[PriceBar],
    peer_contexts: list[PipelineContext] | None = None,
) -> ValuationModelReport:
    """Run exactly the models the caller ordered, with exactly the assumptions supplied.

    The caller provides a ModelOrder specifying:
    - models: which of the 12 to run (tuple of model names)
    - assumptions: per-model assumption overrides (dict keyed by model name)
    - rationale: why the caller chose these (string, required, must be non-empty)
    - declined: models deliberately NOT run, with reasons (tuple of DeclinedModel)

    This function validates the order and runs each named model. Four metrics
    (piotroski_f_score, altman_z_score, beneish_m_score, net_cash_floor) always
    compute regardless of whether the order names them, and appear in always_on.
    Ordered models appear in results (with real output or skip reason).

    Raises ValueError if:
    - order.models names something that is not one of the 12 model names
    - order.rationale is empty or whitespace
    - order.assumptions contains a key that is not a known model name
    """
    peer_contexts = peer_contexts or []

    # Validate the order.
    if not isinstance(order.models, tuple):
        raise ValueError("order.models must be a tuple")
    unknown_models = set(order.models) - set(MODEL_NAMES)
    if unknown_models:
        raise ValueError(f"unknown model names in order.models: {unknown_models}")

    if not order.rationale or not order.rationale.strip():
        raise ValueError("order.rationale must be non-empty and non-whitespace")

    unknown_assumptions = set(order.assumptions.keys()) - set(MODEL_NAMES)
    if unknown_assumptions:
        raise ValueError(f"order.assumptions contains unknown model names: {unknown_assumptions}")

    # Run the ordered models.
    ordered_results: list[ModelResult] = []
    for model_name in order.models:
        assumptions_for_model = order.assumptions.get(model_name, {})
        result = _run_model(model_name, security, derived, fundamentals, prices, peer_contexts, assumptions_for_model)
        ordered_results.append(result)

    # Always compute the four quality/risk metrics.
    always_on_results: list[ModelResult] = []
    for model_name in _ALWAYS_ON_MODELS:
        # Only compute if not already in the ordered set (avoid duplication).
        if model_name not in order.models:
            assumptions_for_model = order.assumptions.get(model_name, {})
            result = _run_model(model_name, security, derived, fundamentals, prices, peer_contexts, assumptions_for_model)
            always_on_results.append(result)

    return ValuationModelReport(
        ticker=security.ticker,
        as_of=derived.as_of,
        results=ordered_results,
        always_on=always_on_results,
        order=order,
    )


def _run_model(
    name: str,
    security: Security,
    derived: ValuationDerived,
    fundamentals: ValuationFundamentals | None,
    prices: list[PriceBar],
    peer_contexts: list[PipelineContext],
    assumptions: dict,
) -> ModelResult:
    """Run a single model with the given assumptions. Return ModelResult with status
    and output or skip_reason."""

    if name == "reverse_dcf":
        # Extract assumptions, pass defaults explicitly if not provided.
        discount_rates = assumptions.get("discount_rates")
        if discount_rates is None:
            return ModelResult(
                model=name,
                group=_GROUP_OF[name],
                status=STATUS_SKIPPED,
                skip_reason="no discount rate supplied in the order",
            )
        return reverse_dcf(
            derived,
            forecast_years=assumptions.get("forecast_years", 10),
            discount_rates=discount_rates,
            terminal_growth=assumptions.get("terminal_growth", 0.025),
            fcf_basis=assumptions.get("fcf_basis", "ttm_fcf"),
        )

    if name == "two_stage_dcf":
        discount_rates = assumptions.get("discount_rates")
        if discount_rates is None:
            return ModelResult(
                model=name,
                group=_GROUP_OF[name],
                status=STATUS_SKIPPED,
                skip_reason="no discount rate supplied in the order",
            )
        return two_stage_dcf(
            derived,
            stage1_years=assumptions.get("stage1_years", 5),
            stage1_growth_rates=assumptions.get("stage1_growth_rates", (0.0, 0.05, 0.10)),
            terminal_growth=assumptions.get("terminal_growth", 0.025),
            discount_rates=discount_rates,
        )

    if name == "owner_earnings":
        discount_rates = assumptions.get("discount_rates")
        if discount_rates is None:
            return ModelResult(
                model=name,
                group=_GROUP_OF[name],
                status=STATUS_SKIPPED,
                skip_reason="no discount rate supplied in the order",
            )
        return owner_earnings_valuation(
            derived,
            growth_rates=assumptions.get("growth_rates", (0.0, 0.05, 0.10)),
            discount_rates=discount_rates,
        )

    if name == "own_history_percentile":
        multiples = assumptions.get("multiples")
        if multiples is None:
            multiples = ("ev_fcf", "ev_sales", "ev_ebitda", "pe")
        return own_history_percentile(fundamentals, derived, prices, multiples=multiples)

    if name == "peer_comparison":
        multiples = assumptions.get("multiples")
        if multiples is None:
            multiples = ("ev_fcf", "ev_sales", "ev_ebitda", "pe")
        return peer_comparison(derived, security, peer_contexts, multiples=multiples)

    if name == "net_cash_floor":
        return net_cash_floor(derived, fundamentals)

    if name == "sum_of_the_parts":
        return sum_of_the_parts(
            derived,
            illiquidity_haircut=assumptions.get("illiquidity_haircut", 0.25),
            include_operating_leases_in_net_debt=assumptions.get("include_operating_leases_in_net_debt", False),
            operating_lease_liabilities=assumptions.get("operating_lease_liabilities"),
        )

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

    raise AssertionError(f"unknown model name: {name}")  # pragma: no cover
