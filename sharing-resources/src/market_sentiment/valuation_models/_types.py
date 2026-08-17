from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date
from typing import Any

# ---------------------------------------------------------------------------
# Group labels — every ModelResult.group is one of these four constants.
# ---------------------------------------------------------------------------

GROUP_CASH_FLOW_INTRINSIC = "cash_flow_intrinsic_value"
GROUP_RELATIVE_VALUATION = "relative_valuation"
GROUP_DOWNSIDE_PROTECTION = "downside_protection"
GROUP_QUALITY_AND_RISK = "quality_and_risk"

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"

# Layer 2 marker echoed on every report — this module is advisory evidence only.
# It must never enter bucket_scores, never change ActionState, never set
# partial_coverage, and must never be able to raise or fail the pipeline.
LAYER_MARKER = "advisory_layer_2_valuation_models"


@dataclass(slots=True)
class ModelResult:
    """The uniform envelope every one of the 12 models (and the router's exclusions)
    returns. ``outputs`` never contains a single bare point estimate for an
    assumption-sensitive model — a range, a grid, or a per-discount-rate list instead.
    A model that cannot run returns status="skipped" with a specific skip_reason —
    never a fabricated number.
    """

    model: str
    group: str
    status: str  # STATUS_OK | STATUS_SKIPPED
    skip_reason: str | None = None
    assumptions: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(asdict(self))


@dataclass(slots=True)
class RouterProfile:
    """The company-profile facts the router computed and used to make its decisions,
    surfaced so a reader can audit *why* the router chose what it chose."""

    fcf_sign: str  # "positive" | "negative" | "unknown"
    fcf_stable: bool | None
    net_cash_to_market_cap: float | None
    non_operating_to_market_cap: float | None
    beta_reliable: bool
    discount_rate_method: str  # "capm_derived_range" | "sector_default_range"
    discount_rates: tuple[float, ...]
    layer: str
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RouterDecision:
    """The router's output: which models to run, and — equally important — which
    were excluded and exactly why, before any model has actually executed."""

    enabled_models: list[str]
    excluded: list[dict[str, str]]  # [{"model": ..., "reason": ...}, ...]
    profile: RouterProfile


@dataclass(slots=True)
class RouterParams:
    """Every threshold the router uses to branch, as an explicit, overridable
    parameter with a documented default. Nothing here is buried in a function body."""

    # net_cash / market_cap above this fraction forces SOTP + net-cash floor and
    # de-emphasises P/E (task spec: "> 30%").
    net_cash_ratio_force_sotp: float = 0.30
    # non_operating_assets / market_cap above this fraction forces SOTP.
    non_operating_assets_materiality: float = 0.05
    # CAPM inputs used only when beta is reliable; the result is still expanded into
    # a +/- range, never passed through as a single discount-rate point.
    risk_free_rate: float = 0.04
    equity_risk_premium: float = 0.05
    capm_spread: float = 0.02
    # Sector-default discount-rate ranges used whenever beta is unreliable/unavailable
    # (the common case on ~6 months of daily bars per architecture.md). Rough,
    # documented, and stated as a range — not a computed fact.
    sector_default_discount_rates: dict[str, tuple[float, ...]] = field(
        default_factory=lambda: {
            "ai_applications": (0.09, 0.11, 0.13),
            "compute": (0.10, 0.12, 0.14),
            "utilities": (0.06, 0.07, 0.08),
            "pharma": (0.07, 0.085, 0.10),
        }
    )
    fallback_discount_rates: tuple[float, ...] = (0.08, 0.10, 0.12)
    # Minimum own-history observations before a percentile is treated as more than
    # directional noise (task spec: "with few quarters a percentile is noise").
    min_reliable_history_observations: int = 8


@dataclass(slots=True)
class ValuationModelReport:
    """The top-level object this module hands to a caller (and, later, to the review
    packet). Layer 2 advisory evidence only — see LAYER_MARKER."""

    ticker: str
    as_of: date
    router: RouterDecision
    results: list[ModelResult]
    layer: str = LAYER_MARKER

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(
            {
                "ticker": self.ticker,
                "as_of": self.as_of,
                "layer": self.layer,
                "router": asdict(self.router),
                "results": [result.to_dict() for result in self.results],
            }
        )


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    return value
