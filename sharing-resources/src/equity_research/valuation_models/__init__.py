"""The valuation MODEL layer — Layer 2 advisory evidence only, built on top of
``equity_research.valuation``'s derived-inputs layer (``ValuationDerived`` /
``ValuationFundamentals``).

Governing principle: Python computes, Python does not assume. Every model takes its
assumptions as explicit parameters with documented defaults — nothing is buried in a
function body. A model's output is never a verdict ("undervalued"); it is numbers,
ranges, and the assumptions that produced them. Where a model is assumption-
sensitive the output is a range or a sensitivity grid, never a single point estimate
(a precise-looking `$94.32` is worse than useless — it looks like a fact when it is
a function of guessed inputs). The LLM agent reading the resulting packet makes the
judgement; this module never does.

Hard architectural rule (matches ``valuation.py`` / ``review_packets.py``'s
``analyst_summary`` pattern exactly): this is Layer 2 advisory evidence. It MUST NOT
enter ``bucket_scores``, MUST NOT change any ``ActionState``, MUST NOT set
``partial_coverage``, and MUST NOT be able to raise into or fail the pipeline. A
model that cannot run returns a structured "skipped + reason" ``ModelResult``,
never a fabricated number.

Entry point: ``run_order(security, order, derived, fundamentals, prices, peer_contexts)``
runs exactly the models the caller orders, with exactly the assumptions supplied,
and returns a ``ValuationModelReport`` whose ``results`` list contains ``ModelResult``
entries for the ordered models, and whose ``always_on`` list contains the four
always-computed quality/risk metrics (piotroski_f_score, altman_z_score,
beneish_m_score, net_cash_floor), which are pure formulas and must not be quietly
omitted.

Module layout (a package, not a single file, because 12 models plus routing don't
fit legibly in one module):

- ``_types``     ModelResult / ModelOrder / DeclinedModel / ValuationModelReport
- ``_series``    shared time-series reconstruction over ConceptHistory / PriceBar
- ``_math``      bisection solver + growing-FCF / Gordon-growth present-value helpers
- ``_dcf``       Group 1: reverse_dcf, two_stage_dcf, owner_earnings_valuation
- ``_relative``  Group 2: own_history_percentile, peer_comparison
- ``_downside``  Group 3: net_cash_floor, sum_of_the_parts, cash_runway
- ``_quality``   Group 4: piotroski_f_score, altman_z_score, beneish_m_score
- ``_scenarios`` Group 4 (cont.): three_scenario_expected_value, ScenarioParams
- ``_router``    run_order: the entry point that runs ordered models
"""
from __future__ import annotations

from equity_research.valuation_models._dcf import owner_earnings_valuation, reverse_dcf, two_stage_dcf
from equity_research.valuation_models._downside import cash_runway, net_cash_floor, sum_of_the_parts
from equity_research.valuation_models._quality import altman_z_score, beneish_m_score, piotroski_f_score
from equity_research.valuation_models._relative import own_history_percentile, peer_comparison
from equity_research.valuation_models._router import MODEL_NAMES, run_order
from equity_research.valuation_models._scenarios import ScenarioParams, three_scenario_expected_value
from equity_research.valuation_models._types import (
    GROUP_CASH_FLOW_INTRINSIC,
    GROUP_DOWNSIDE_PROTECTION,
    GROUP_QUALITY_AND_RISK,
    GROUP_RELATIVE_VALUATION,
    LAYER_MARKER,
    DeclinedModel,
    ModelOrder,
    ModelResult,
    RouterParams,
    STATUS_OK,
    STATUS_SKIPPED,
    ValuationModelReport,
)

__all__ = [
    "MODEL_NAMES",
    "run_order",
    "reverse_dcf",
    "two_stage_dcf",
    "owner_earnings_valuation",
    "own_history_percentile",
    "peer_comparison",
    "net_cash_floor",
    "sum_of_the_parts",
    "cash_runway",
    "piotroski_f_score",
    "altman_z_score",
    "beneish_m_score",
    "three_scenario_expected_value",
    "ScenarioParams",
    "ModelResult",
    "DeclinedModel",
    "ModelOrder",
    "RouterParams",
    "ValuationModelReport",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "GROUP_CASH_FLOW_INTRINSIC",
    "GROUP_RELATIVE_VALUATION",
    "GROUP_DOWNSIDE_PROTECTION",
    "GROUP_QUALITY_AND_RISK",
    "LAYER_MARKER",
]
