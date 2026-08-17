"""Group 4 (continued) — three-scenario expected value."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from market_sentiment.models import ValuationDerived
from market_sentiment.valuation_models._math import pv_growing_fcf_with_terminal_value
from market_sentiment.valuation_models._types import GROUP_QUALITY_AND_RISK, ModelResult, STATUS_OK, STATUS_SKIPPED


@dataclass(slots=True, frozen=True)
class ScenarioParams:
    """Every field here is an explicit assumption for one scenario — never hardcoded
    inside the model body. Defaults are documented on ``three_scenario_expected_value``."""

    fcf_growth_rate: float
    discount_rate: float
    terminal_growth: float = 0.025
    forecast_years: int = 5
    fcf_basis: str = "ttm_fcf"


_DEFAULT_BEAR = ScenarioParams(fcf_growth_rate=-0.05, discount_rate=0.12, terminal_growth=0.015)
_DEFAULT_BASE = ScenarioParams(fcf_growth_rate=0.05, discount_rate=0.10, terminal_growth=0.025)
_DEFAULT_BULL = ScenarioParams(fcf_growth_rate=0.15, discount_rate=0.09, terminal_growth=0.030)
_DEFAULT_PROBABILITIES: tuple[float, float, float] = (0.25, 0.50, 0.25)


def _scenario_value(derived: ValuationDerived, params: ScenarioParams) -> tuple[float | None, str | None]:
    fcf_field = getattr(derived, params.fcf_basis)
    if fcf_field.value is None:
        return None, f"{params.fcf_basis} unavailable: {fcf_field.reason or 'no data'}"
    if fcf_field.value <= 0:
        return None, f"{params.fcf_basis} is {fcf_field.value:,.1f} (<= 0) — scenario DCF undefined"
    ev = pv_growing_fcf_with_terminal_value(
        fcf_field.value, params.fcf_growth_rate, params.discount_rate, params.forecast_years, params.terminal_growth
    )
    if ev is None:
        return None, f"discount_rate ({params.discount_rate}) <= terminal_growth ({params.terminal_growth}) — invalid"
    net_cash = derived.net_cash.value
    shares = derived.shares_used.value
    if net_cash is None or shares is None or shares <= 0:
        return None, "net_cash or shares_used unavailable"
    equity_value = ev + net_cash
    return equity_value / shares, None


def three_scenario_expected_value(
    derived: ValuationDerived,
    bear: ScenarioParams = _DEFAULT_BEAR,
    base: ScenarioParams = _DEFAULT_BASE,
    bull: ScenarioParams = _DEFAULT_BULL,
    probabilities: tuple[float, float, float] = _DEFAULT_PROBABILITIES,
) -> ModelResult:
    """Bear / base / bull single-point DCF valuations (via the same growing-FCF +
    terminal-value machinery as reverse_dcf, but forward — each scenario states its
    own explicit growth/discount/terminal-growth assumptions) combined with explicit
    probabilities into a probability-weighted value and the implied up/down
    asymmetry versus the current price.

    Defaults are documented, ordinary assumptions, not calibrated forecasts:
    bear = -5% growth / 12% discount / 1.5% terminal; base = +5% / 10% / 2.5%; bull =
    +15% / 9% / 3.0%; probabilities 25/50/25. Override every one of them — they are
    parameters precisely so a caller with a specific view does not have to accept
    these defaults silently.
    """
    p_bear, p_base, p_bull = probabilities
    if abs((p_bear + p_base + p_bull) - 1.0) > 1e-6:
        return ModelResult(
            model="three_scenario_expected_value",
            group=GROUP_QUALITY_AND_RISK,
            status=STATUS_SKIPPED,
            skip_reason=f"probabilities must sum to 1.0, got {p_bear + p_base + p_bull}",
        )

    scenarios = {}
    for name, params in (("bear", bear), ("base", base), ("bull", bull)):
        value, reason = _scenario_value(derived, params)
        scenarios[name] = {"per_share_value": value, "reason": reason, "assumptions": asdict(params)}

    unresolved = [name for name, entry in scenarios.items() if entry["per_share_value"] is None]
    if unresolved:
        return ModelResult(
            model="three_scenario_expected_value",
            group=GROUP_QUALITY_AND_RISK,
            status=STATUS_SKIPPED,
            skip_reason=f"cannot compute scenario value(s) for: {unresolved} "
            f"({[scenarios[n]['reason'] for n in unresolved]})",
        )

    weighted_value = (
        p_bear * scenarios["bear"]["per_share_value"]
        + p_base * scenarios["base"]["per_share_value"]
        + p_bull * scenarios["bull"]["per_share_value"]
    )

    market_cap = derived.market_cap.value
    shares = derived.shares_used.value
    current_price = (market_cap / shares) if market_cap is not None and shares else None

    outputs = {
        "scenarios": scenarios,
        "probabilities": {"bear": p_bear, "base": p_base, "bull": p_bull},
        "probability_weighted_value": weighted_value,
        "current_price": current_price,
    }
    if current_price:
        upside_pct = (scenarios["bull"]["per_share_value"] / current_price - 1) * 100
        downside_pct = (scenarios["bear"]["per_share_value"] / current_price - 1) * 100
        expected_return_pct = (weighted_value / current_price - 1) * 100
        outputs["upside_to_bull_pct"] = upside_pct
        outputs["downside_to_bear_pct"] = downside_pct
        outputs["expected_return_pct"] = expected_return_pct
        outputs["up_down_asymmetry_ratio"] = (abs(upside_pct) / abs(downside_pct)) if downside_pct else None

    return ModelResult(
        model="three_scenario_expected_value",
        group=GROUP_QUALITY_AND_RISK,
        status=STATUS_OK,
        assumptions={"bear": asdict(bear), "base": asdict(base), "bull": asdict(bull), "probabilities": list(probabilities)},
        outputs=outputs,
        caveats=[
            "Every scenario's growth rate, discount rate, and terminal growth — and every probability weight — "
            "is an explicit, overridable parameter with a documented default, never a hardcoded silent choice.",
            "The probability-weighted value is not a price target; the up/down asymmetry is the more useful "
            "number for judging whether the current pullback offers a favourable risk/reward, which is the "
            "point of this model for this project.",
        ],
    )
