"""Tests for market_sentiment.valuation_models — the valuation MODEL layer built on
top of valuation.py's derived-inputs layer.

No live network: everything here is pure computation over in-memory ValuationDerived
/ ValuationFundamentals / PriceBar / Security fixtures, following
test_valuation_derived.py's style. Each model has at least one case with
hand-computed (or formula-re-derived-in-the-test) expected values.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from market_sentiment.models import (
    BetaEstimate,
    ConceptDatapoint,
    ConceptHistory,
    Layer,
    PipelineContext,
    PriceBar,
    ProvenancedValue,
    Security,
    ValuationDerived,
    ValuationFundamentals,
)
from market_sentiment import valuation_models as vm
from market_sentiment.valuation_models._math import bisect, gordon_growth_value, pv_growing_fcf_with_terminal_value
from market_sentiment.valuation_models._scenarios import ScenarioParams
from market_sentiment.valuation_models._types import RouterParams


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _pv(value: float | None, reason: str | None = None) -> ProvenancedValue:
    return ProvenancedValue(value=value, reason=reason)


def _derived(**overrides) -> ValuationDerived:
    """A ValuationDerived with every ProvenancedValue field defaulted to
    value=None, overridable by keyword with a raw number (wrapped automatically)."""
    base = {
        "ticker": "TEST",
        "as_of": date(2026, 6, 30),
        "shares_used": _pv(None),
        "market_cap": _pv(None),
        "cash_and_equivalents": _pv(None),
        "short_term_investments": _pv(None),
        "long_term_investments": _pv(None),
        "total_liquid_assets": _pv(None),
        "non_operating_assets": _pv(None),
        "total_debt": _pv(None),
        "net_cash": _pv(None),
        "enterprise_value": _pv(None),
        "ttm_revenue": _pv(None),
        "ttm_gross_profit": _pv(None),
        "ttm_operating_income": _pv(None),
        "ttm_net_income": _pv(None),
        "ttm_operating_cashflow": _pv(None),
        "ttm_capex": _pv(None),
        "ttm_sbc": _pv(None),
        "ttm_da": _pv(None),
        "ttm_fcf": _pv(None),
        "ttm_fcf_ex_sbc": _pv(None),
        "ttm_ebitda": _pv(None),
        "ttm_income_tax_expense": _pv(None),
        "ttm_pretax_income_implied": _pv(None),
        "effective_tax_rate": _pv(None),
        "book_value": _pv(None),
        "working_capital": _pv(None),
        "current_ratio": _pv(None),
        "beta": BetaEstimate(beta=None, observations=0, r_squared=None, reliable=False, reason="no data"),
        "data_gaps": [],
    }
    for key, value in overrides.items():
        if key == "beta" or key == "data_gaps" or isinstance(value, ProvenancedValue):
            base[key] = value
        else:
            base[key] = _pv(value)
    return ValuationDerived(**base)


def _security(ticker: str = "ZM", layer: Layer = Layer.AI_APPLICATIONS) -> Security:
    return Security(ticker=ticker, name=ticker, layer=layer, benchmark="QQQ")


def _fundamentals(concepts: dict[str, ConceptHistory] | None = None) -> ValuationFundamentals:
    return ValuationFundamentals(
        ticker="TEST", cik="1", source="sec_companyfacts", source_url=None, ingested_at=None,
        concepts=concepts or {},
    )


def _quarterly_history(concept: str, tag: str, values: list[tuple[date, float]]) -> ConceptHistory:
    datapoints = [ConceptDatapoint(end=end, filed=end, value=value, form="10-Q") for end, value in values]
    return ConceptHistory(concept=concept, tag=tag, unit="USD", datapoints=datapoints)


def _ttm_quarters(concept: str, tag: str, base: date, current_total: float, prior_total: float) -> ConceptHistory:
    """8 quarterly datapoints, 91 days apart: points[0:4] sum to current_total,
    points[4:8] sum to prior_total — so rolling_ttm_series's windows[0] and
    windows[4] land exactly on current_total / prior_total, ~364 days apart."""
    values = []
    for i in range(4):
        values.append((base - timedelta(days=91 * i), current_total / 4))
    for i in range(4, 8):
        values.append((base - timedelta(days=91 * i), prior_total / 4))
    return _quarterly_history(concept, tag, values)


def _two_point_instant(concept: str, tag: str, base: date, current: float, prior: float) -> ConceptHistory:
    return _quarterly_history(concept, tag, [(base, current), (base - timedelta(days=365), prior)])


def _bars(ticker: str, closes: list[tuple[date, float]]) -> list[PriceBar]:
    return [
        PriceBar(ticker=ticker, trading_date=d, open=c, high=c, low=c, close=c, volume=1000, source="stub")
        for d, c in closes
    ]


# ---------------------------------------------------------------------------
# _math
# ---------------------------------------------------------------------------

class MathHelpersTests(TestCase):
    def test_bisect_finds_root_of_simple_linear_function(self) -> None:
        # f(x) = x - 0.3, root at 0.3
        root = bisect(lambda x: x - 0.3, -1.0, 1.0)
        self.assertAlmostEqual(root, 0.3, places=5)

    def test_bisect_returns_none_when_no_sign_change(self) -> None:
        self.assertIsNone(bisect(lambda x: x + 5, 0.0, 1.0))

    def test_gordon_growth_value_hand_computed(self) -> None:
        # 100*(1.02)/(0.10-0.02) = 102/0.08 = 1275
        self.assertAlmostEqual(gordon_growth_value(100.0, 0.02, 0.10), 1275.0, places=6)

    def test_gordon_growth_value_none_when_rate_not_above_growth(self) -> None:
        self.assertIsNone(gordon_growth_value(100.0, 0.10, 0.10))

    def test_pv_growing_fcf_hand_computed_single_year(self) -> None:
        # fcf0=100, g=0.05, r=0.10, n=1, tg=0.02
        # PV = 100*1.05/1.10 + [100*1.05*1.02/(0.10-0.02)]/1.10
        pv = pv_growing_fcf_with_terminal_value(100.0, 0.05, 0.10, 1, 0.02)
        expected = (100 * 1.05) / 1.10 + (100 * 1.05 * 1.02 / 0.08) / 1.10
        self.assertAlmostEqual(pv, expected, places=6)
        self.assertAlmostEqual(pv, 1312.5, places=1)


# ---------------------------------------------------------------------------
# Group 1 — reverse DCF, two-stage DCF, owner earnings
# ---------------------------------------------------------------------------

class ReverseDcfTests(TestCase):
    def test_hand_computed_implied_growth_recovered(self) -> None:
        # PV(g) = 1250*(1+g) for fcf0=100, r=0.10, tg=0.02, n=1 (derived in
        # test_pv_growing_fcf_hand_computed_single_year). Target EV at g=0.05 is
        # 1250*1.05 = 1312.5 — the solver must recover g=0.05.
        derived = _derived(ttm_fcf=100.0, enterprise_value=1312.5)
        result = vm.reverse_dcf(derived, forecast_years=1, discount_rates=(0.10,), terminal_growth=0.02)
        self.assertEqual(result.status, vm.STATUS_OK)
        implied = result.outputs["implied_growth_by_discount_rate"][0]["implied_fcf_growth"]
        self.assertAlmostEqual(implied, 0.05, places=4)

    def test_reports_across_multiple_discount_rates(self) -> None:
        derived = _derived(ttm_fcf=100.0, enterprise_value=2000.0)
        result = vm.reverse_dcf(derived, discount_rates=(0.08, 0.10, 0.12))
        self.assertEqual(len(result.outputs["implied_growth_by_discount_rate"]), 3)

    def test_skips_on_negative_fcf(self) -> None:
        derived = _derived(ttm_fcf=-50.0, enterprise_value=1000.0)
        result = vm.reverse_dcf(derived)
        self.assertEqual(result.status, vm.STATUS_SKIPPED)
        self.assertIn("<= 0", result.skip_reason)

    def test_skips_on_missing_fcf(self) -> None:
        derived = _derived(enterprise_value=1000.0)
        result = vm.reverse_dcf(derived)
        self.assertEqual(result.status, vm.STATUS_SKIPPED)

    def test_skips_on_nonpositive_enterprise_value(self) -> None:
        derived = _derived(ttm_fcf=100.0, enterprise_value=-5.0)
        result = vm.reverse_dcf(derived)
        self.assertEqual(result.status, vm.STATUS_SKIPPED)
        self.assertIn("net cash exceeds market cap", result.skip_reason)


class TwoStageDcfTests(TestCase):
    def test_hand_computed_cell(self) -> None:
        derived = _derived(ttm_fcf=100.0, net_cash=50.0, shares_used=10.0)
        result = vm.two_stage_dcf(
            derived, stage1_years=1, stage1_growth_rates=(0.05,), terminal_growth=0.02, discount_rates=(0.10,)
        )
        self.assertEqual(result.status, vm.STATUS_OK)
        cell = result.outputs["grid"][0]["cells"][0]
        self.assertAlmostEqual(cell["enterprise_value"], 1312.5, places=1)
        # equity = 1312.5 + 50 = 1362.5; per share = 136.25
        self.assertAlmostEqual(cell["implied_price_per_share"], 136.25, places=2)

    def test_returns_a_grid_not_a_point(self) -> None:
        derived = _derived(ttm_fcf=100.0, net_cash=50.0, shares_used=10.0)
        result = vm.two_stage_dcf(derived, stage1_growth_rates=(0.0, 0.1, 0.2), discount_rates=(0.08, 0.12))
        self.assertEqual(len(result.outputs["grid"]), 2)
        for row in result.outputs["grid"]:
            self.assertEqual(len(row["cells"]), 3)

    def test_skips_on_negative_fcf(self) -> None:
        derived = _derived(ttm_fcf=-10.0)
        result = vm.two_stage_dcf(derived)
        self.assertEqual(result.status, vm.STATUS_SKIPPED)


class OwnerEarningsTests(TestCase):
    def test_hand_computed_headline_vs_adjusted_gap(self) -> None:
        derived = _derived(ttm_fcf=100.0, ttm_fcf_ex_sbc=60.0, ttm_sbc=40.0, net_cash=50.0, shares_used=10.0)
        result = vm.owner_earnings_valuation(derived, growth_rates=(0.02,), discount_rates=(0.10,))
        self.assertEqual(result.status, vm.STATUS_OK)
        self.assertAlmostEqual(result.outputs["sbc_drag_ratio"], 0.6, places=6)
        # headline: gordon_growth_value(100,0.02,0.10) = 100*1.02/0.08 = 1275; equity=1325; /10=132.5
        headline_cell = result.outputs["headline_valuation_grid"][0]["cells"][0]
        self.assertAlmostEqual(headline_cell["enterprise_value"], 1275.0, places=2)
        self.assertAlmostEqual(headline_cell["implied_price_per_share"], 132.5, places=2)
        # sbc-adjusted: gordon_growth_value(60,0.02,0.10) = 60*1.02/0.08 = 765; equity=815; /10=81.5
        adjusted_cell = result.outputs["sbc_adjusted_valuation_grid"][0]["cells"][0]
        self.assertAlmostEqual(adjusted_cell["enterprise_value"], 765.0, places=2)
        self.assertAlmostEqual(adjusted_cell["implied_price_per_share"], 81.5, places=2)

    def test_skips_entirely_when_both_bases_missing(self) -> None:
        derived = _derived()
        result = vm.owner_earnings_valuation(derived)
        self.assertEqual(result.status, vm.STATUS_SKIPPED)


# ---------------------------------------------------------------------------
# Group 2 — own-history percentile, peer comparison
# ---------------------------------------------------------------------------

class OwnHistoryPercentileTests(TestCase):
    def test_hand_computed_percentile_from_constant_revenue_rising_price(self) -> None:
        base = date(2026, 4, 30)
        # 5 quarter-ends, 91 days apart, revenue=100/quarter -> every TTM window = 400.
        dates = [base - timedelta(days=91 * i) for i in range(8)]
        revenue = _quarterly_history("revenue", "Revenues", [(d, 100.0) for d in dates])
        # cash=0 at every TTM-end date (required by _historical_multiples; must not be omitted).
        cash = _quarterly_history("cash_and_equivalents", "CashAndCashEquivalentsAtCarryingValue", [(d, 0.0) for d in dates[:5]])
        shares = _quarterly_history("diluted_weighted_avg_shares", "WeightedAverageNumberOfDilutedSharesOutstanding", [(d, 10.0) for d in dates[:5]])
        fundamentals = _fundamentals({"revenue": revenue, "cash_and_equivalents": cash, "diluted_weighted_avg_shares": shares})

        # Prices at the 5 TTM-end dates: 104, 103, 102, 101, 100 (most-recent-first).
        prices = _bars("TEST", [(dates[0], 104.0), (dates[1], 103.0), (dates[2], 102.0), (dates[3], 101.0), (dates[4], 100.0)])
        # EV = price*10 (net_cash=0); ev_sales = EV/400 = price/40.
        # series (most-recent-first): [2.6, 2.575, 2.55, 2.525, 2.5]

        # Current derived: price=102 -> ev_sales = 1020/400 = 2.55 (matches the 3rd
        # historical point exactly) -> rank = count(v <= 2.55) = 3 of 5 -> 60%.
        current_derived = _derived(enterprise_value=1020.0, market_cap=1020.0, ttm_revenue=400.0)

        result = vm.own_history_percentile(fundamentals, current_derived, prices, multiples=("ev_sales",))
        self.assertEqual(result.status, vm.STATUS_OK)
        entry = result.outputs["multiples"]["ev_sales"]
        self.assertEqual(entry["observations"], 5)
        self.assertAlmostEqual(entry["percentile"], 60.0, places=6)
        self.assertIn("reliability_note", entry)  # 5 < default min_reliable_observations (8)

    def test_skips_metric_with_no_reconstructable_history(self) -> None:
        fundamentals = _fundamentals({})
        current_derived = _derived(ttm_revenue=100.0, enterprise_value=500.0)
        result = vm.own_history_percentile(fundamentals, current_derived, [], multiples=("ev_sales",))
        self.assertEqual(result.status, vm.STATUS_SKIPPED)

    def test_skips_whole_model_when_no_fundamentals(self) -> None:
        result = vm.own_history_percentile(None, _derived(), [], multiples=("ev_sales",))
        self.assertEqual(result.status, vm.STATUS_SKIPPED)


class PeerComparisonTests(TestCase):
    def test_hand_computed_peer_median_and_relative_position(self) -> None:
        subject = _security("ZM", Layer.AI_APPLICATIONS)
        subject_derived = _derived(enterprise_value=1000.0, ttm_revenue=200.0)  # ev_sales = 5.0

        peer_a_derived = _derived(enterprise_value=800.0, ttm_revenue=100.0)  # ev_sales = 8.0
        peer_b_derived = _derived(enterprise_value=600.0, ttm_revenue=150.0)  # ev_sales = 4.0
        peer_a_ctx = _peer_context("PEERA", Layer.AI_APPLICATIONS, peer_a_derived)
        peer_b_ctx = _peer_context("PEERB", Layer.AI_APPLICATIONS, peer_b_derived)
        # peer median of [8.0, 4.0] = 6.0; subject 5.0 vs median 6.0 -> -16.666...%

        result = vm.peer_comparison(subject_derived, subject, [peer_a_ctx, peer_b_ctx], multiples=("ev_sales",))
        self.assertEqual(result.status, vm.STATUS_OK)
        entry = result.outputs["multiples"]["ev_sales"]
        self.assertEqual(entry["peer_count"], 2)
        self.assertAlmostEqual(entry["peer_median"], 6.0, places=6)
        self.assertAlmostEqual(entry["subject_vs_peer_median_pct"], (5.0 / 6.0 - 1) * 100, places=6)

    def test_always_warns_cheap_multiple_in_expensive_sector(self) -> None:
        subject = _security("ZM")
        peer_ctx = _peer_context("PEERA", Layer.AI_APPLICATIONS, _derived(enterprise_value=800.0, ttm_revenue=100.0))
        result = vm.peer_comparison(_derived(enterprise_value=1000.0, ttm_revenue=200.0), subject, [peer_ctx])
        joined = " ".join(result.caveats)
        self.assertIn("cheap multiple", joined)

    def test_skips_when_no_same_layer_peers(self) -> None:
        subject = _security("ZM", Layer.AI_APPLICATIONS)
        peer_ctx = _peer_context("UTIL1", Layer.UTILITIES, _derived(enterprise_value=800.0, ttm_revenue=100.0))
        result = vm.peer_comparison(_derived(enterprise_value=1000.0, ttm_revenue=200.0), subject, [peer_ctx])
        self.assertEqual(result.status, vm.STATUS_SKIPPED)


def _peer_context(ticker: str, layer: Layer, derived: ValuationDerived) -> PipelineContext:
    return PipelineContext(
        security=_security(ticker, layer),
        benchmark_ticker="QQQ",
        prices=[],
        benchmark_prices=[],
        official_events=[],
        fundamentals=None,
        macro=[],
        source_statuses=[],
        valuation_derived=derived,
    )


# ---------------------------------------------------------------------------
# Group 3 — net-cash floor, sum-of-the-parts, cash runway
# ---------------------------------------------------------------------------

class NetCashFloorTests(TestCase):
    def test_hand_computed(self) -> None:
        derived = _derived(total_liquid_assets=1000.0, shares_used=100.0, market_cap=1200.0)
        fundamentals = _fundamentals({
            "total_current_assets": _quarterly_history("total_current_assets", "AssetsCurrent", [(date(2026, 6, 30), 1500.0)]),
            "total_liabilities": _quarterly_history("total_liabilities", "Liabilities", [(date(2026, 6, 30), 800.0)]),
        })
        result = vm.net_cash_floor(derived, fundamentals)
        self.assertEqual(result.status, vm.STATUS_OK)
        out = result.outputs
        self.assertAlmostEqual(out["liquid_assets_per_share"], 10.0, places=6)
        self.assertAlmostEqual(out["current_price"], 12.0, places=6)
        self.assertAlmostEqual(out["ncav"], 700.0, places=6)
        self.assertAlmostEqual(out["ncav_per_share"], 7.0, places=6)
        self.assertAlmostEqual(out["price_to_liquid_assets_per_share"], 1.2, places=6)
        self.assertAlmostEqual(out["price_to_ncav_per_share"], 12.0 / 7.0, places=6)
        self.assertAlmostEqual(out["graham_two_thirds_ncav_per_share"], 7.0 * 2 / 3, places=6)

    def test_skips_when_liquid_assets_missing(self) -> None:
        result = vm.net_cash_floor(_derived(shares_used=100.0), _fundamentals())
        self.assertEqual(result.status, vm.STATUS_SKIPPED)

    def test_uses_total_liquid_assets_not_cash_alone_by_construction(self) -> None:
        # total_liquid_assets is the only input consumed for the liquid floor —
        # cash_and_equivalents alone is never read by this model.
        derived = _derived(total_liquid_assets=7721.0, cash_and_equivalents=891.0, shares_used=300.0, market_cap=30000.0)
        result = vm.net_cash_floor(derived, _fundamentals())
        self.assertAlmostEqual(result.outputs["liquid_assets_per_share"], 7721.0 / 300.0, places=4)


class SumOfThePartsTests(TestCase):
    def test_hand_computed_inverse_residual(self) -> None:
        derived = _derived(market_cap=1000.0, net_cash=300.0, non_operating_assets=200.0, shares_used=50.0)
        result = vm.sum_of_the_parts(derived, illiquidity_haircut=0.30)
        self.assertEqual(result.status, vm.STATUS_OK)
        out = result.outputs
        self.assertAlmostEqual(out["non_operating_assets_after_haircut"], 140.0, places=6)
        # implied core = 1000 - 300 - 140 = 560
        self.assertAlmostEqual(out["implied_core_business_value"], 560.0, places=6)
        self.assertAlmostEqual(out["implied_core_business_value_per_share"], 11.2, places=6)

    def test_operating_lease_treatment_is_explicit_and_documented(self) -> None:
        derived = _derived(market_cap=1000.0, net_cash=300.0, shares_used=50.0)
        default_result = vm.sum_of_the_parts(derived)
        self.assertFalse(default_result.assumptions["include_operating_leases_in_net_debt"])
        self.assertEqual(default_result.outputs["net_cash_after_lease_treatment"], 300.0)

        included_result = vm.sum_of_the_parts(
            derived, include_operating_leases_in_net_debt=True, operating_lease_liabilities=60.2
        )
        self.assertAlmostEqual(included_result.outputs["net_cash_after_lease_treatment"], 300.0 - 60.2, places=6)

    def test_degrades_gracefully_when_non_operating_assets_missing(self) -> None:
        derived = _derived(market_cap=1000.0, net_cash=300.0, shares_used=50.0)
        result = vm.sum_of_the_parts(derived)
        self.assertEqual(result.status, vm.STATUS_OK)
        self.assertAlmostEqual(result.outputs["implied_core_business_value"], 700.0, places=6)


class CashRunwayTests(TestCase):
    def test_hand_computed(self) -> None:
        derived = _derived(ttm_fcf=-120.0, total_liquid_assets=100.0)
        result = vm.cash_runway(derived)
        self.assertEqual(result.status, vm.STATUS_OK)
        self.assertAlmostEqual(result.outputs["monthly_burn_rate"], 10.0, places=6)
        self.assertAlmostEqual(result.outputs["runway_months"], 10.0, places=6)

    def test_skips_when_fcf_non_negative(self) -> None:
        result = vm.cash_runway(_derived(ttm_fcf=10.0, total_liquid_assets=100.0))
        self.assertEqual(result.status, vm.STATUS_SKIPPED)


# ---------------------------------------------------------------------------
# Group 4 — Piotroski, Altman, Beneish, three-scenario
# ---------------------------------------------------------------------------

class PiotroskiTests(TestCase):
    def test_hand_computed_all_criteria_pass(self) -> None:
        base = date(2026, 4, 30)
        fundamentals = _fundamentals({
            "revenue": _ttm_quarters("revenue", "Revenues", base, current_total=1000.0, prior_total=800.0),
            "gross_profit": _ttm_quarters("gross_profit", "GrossProfit", base, current_total=600.0, prior_total=400.0),  # margin 0.6 > 0.5
            "net_income": _ttm_quarters("net_income", "NetIncomeLoss", base, current_total=100.0, prior_total=40.0),
            "operating_cashflow": _ttm_quarters("operating_cashflow", "NetCashProvidedByUsedInOperatingActivities", base, current_total=150.0, prior_total=100.0),
            "total_assets": _two_point_instant("total_assets", "Assets", base, current=1000.0, prior=1000.0),  # ROA_c=100/1000=.1 > ROA_p=40/1000=.04
            "long_term_debt": _two_point_instant("long_term_debt", "LongTermDebt", base, current=100.0, prior=200.0),  # leverage decreased
            "total_current_assets": _two_point_instant("total_current_assets", "AssetsCurrent", base, current=500.0, prior=300.0),
            "total_current_liabilities": _two_point_instant("total_current_liabilities", "LiabilitiesCurrent", base, current=200.0, prior=200.0),  # CR up: 2.5 > 1.5
            "diluted_weighted_avg_shares": _two_point_instant("diluted_weighted_avg_shares", "WeightedAverageNumberOfDilutedSharesOutstanding", base, current=100.0, prior=100.0),  # no new shares
        })
        result = vm.piotroski_f_score(fundamentals)
        self.assertEqual(result.status, vm.STATUS_OK)
        self.assertEqual(result.outputs["max_score"], 9)
        self.assertEqual(result.outputs["score"], 9, result.outputs["criteria"])

    def test_partial_score_when_leverage_data_missing(self) -> None:
        base = date(2026, 4, 30)
        fundamentals = _fundamentals({
            "revenue": _ttm_quarters("revenue", "Revenues", base, current_total=1000.0, prior_total=800.0),
            "gross_profit": _ttm_quarters("gross_profit", "GrossProfit", base, current_total=600.0, prior_total=400.0),
            "net_income": _ttm_quarters("net_income", "NetIncomeLoss", base, current_total=100.0, prior_total=40.0),
            "operating_cashflow": _ttm_quarters("operating_cashflow", "NetCashProvidedByUsedInOperatingActivities", base, current_total=150.0, prior_total=100.0),
            "total_assets": _two_point_instant("total_assets", "Assets", base, current=1000.0, prior=1000.0),
            # long_term_debt intentionally omitted -> leverage_decreased must be None
            "total_current_assets": _two_point_instant("total_current_assets", "AssetsCurrent", base, current=500.0, prior=300.0),
            "total_current_liabilities": _two_point_instant("total_current_liabilities", "LiabilitiesCurrent", base, current=200.0, prior=200.0),
            "diluted_weighted_avg_shares": _two_point_instant("diluted_weighted_avg_shares", "WeightedAverageNumberOfDilutedSharesOutstanding", base, current=100.0, prior=100.0),
        })
        result = vm.piotroski_f_score(fundamentals)
        self.assertEqual(result.status, vm.STATUS_OK)
        self.assertIsNone(result.outputs["criteria"]["leverage_decreased"])
        self.assertEqual(result.outputs["max_score"], 8)

    def test_skips_when_no_fundamentals(self) -> None:
        self.assertEqual(vm.piotroski_f_score(None).status, vm.STATUS_SKIPPED)


class AltmanZScoreTests(TestCase):
    def test_hand_computed_z_double_prime(self) -> None:
        derived = _derived(working_capital=200.0, ttm_operating_income=150.0, book_value=600.0)
        fundamentals = _fundamentals({
            "total_assets": _quarterly_history("total_assets", "Assets", [(date(2026, 6, 30), 1000.0)]),
            "total_liabilities": _quarterly_history("total_liabilities", "Liabilities", [(date(2026, 6, 30), 400.0)]),
            "retained_earnings": _quarterly_history("retained_earnings", "RetainedEarningsAccumulatedDeficit", [(date(2026, 6, 30), 300.0)]),
        })
        result = vm.altman_z_score(derived, fundamentals)
        self.assertEqual(result.status, vm.STATUS_OK)
        # x1=0.2, x2=0.3, x3=0.15, x4=1.5
        # z = 6.56*0.2 + 3.26*0.3 + 6.72*0.15 + 1.05*1.5 = 1.312+0.978+1.008+1.575=4.873
        self.assertAlmostEqual(result.outputs["z_score"], 6.56 * 0.2 + 3.26 * 0.3 + 6.72 * 0.15 + 1.05 * 1.5, places=6)
        self.assertAlmostEqual(result.outputs["z_score"], 4.873, places=3)
        self.assertEqual(result.outputs["zone"], "safe")
        self.assertEqual(result.assumptions["variant"], "Z'' (Z-double-prime, non-manufacturer)")

    def test_distress_zone_hand_computed(self) -> None:
        # x1=0, x2=0, x3=0, x4 small -> z well below 1.1
        derived = _derived(working_capital=0.0, ttm_operating_income=0.0, book_value=10.0)
        fundamentals = _fundamentals({
            "total_assets": _quarterly_history("total_assets", "Assets", [(date(2026, 6, 30), 1000.0)]),
            "total_liabilities": _quarterly_history("total_liabilities", "Liabilities", [(date(2026, 6, 30), 900.0)]),
            "retained_earnings": _quarterly_history("retained_earnings", "RetainedEarningsAccumulatedDeficit", [(date(2026, 6, 30), 0.0)]),
        })
        result = vm.altman_z_score(derived, fundamentals)
        self.assertEqual(result.outputs["zone"], "distress")

    def test_skips_when_total_liabilities_missing(self) -> None:
        derived = _derived(working_capital=200.0, ttm_operating_income=150.0, book_value=600.0)
        fundamentals = _fundamentals({
            "total_assets": _quarterly_history("total_assets", "Assets", [(date(2026, 6, 30), 1000.0)]),
        })
        result = vm.altman_z_score(derived, fundamentals)
        self.assertEqual(result.status, vm.STATUS_SKIPPED)
        self.assertIn("total_liabilities", result.skip_reason)

    def test_utilities_layer_gets_extra_caveat(self) -> None:
        derived = _derived(working_capital=200.0, ttm_operating_income=150.0, book_value=600.0)
        fundamentals = _fundamentals({
            "total_assets": _quarterly_history("total_assets", "Assets", [(date(2026, 6, 30), 1000.0)]),
            "total_liabilities": _quarterly_history("total_liabilities", "Liabilities", [(date(2026, 6, 30), 400.0)]),
            "retained_earnings": _quarterly_history("retained_earnings", "RetainedEarningsAccumulatedDeficit", [(date(2026, 6, 30), 300.0)]),
        })
        result = vm.altman_z_score(derived, fundamentals, layer=Layer.UTILITIES)
        self.assertTrue(any("regulated utilities" in c for c in result.caveats))


class BeneishMScoreTests(TestCase):
    def test_skips_when_required_concepts_not_extracted(self) -> None:
        # This is the realistic case: sources/sec.py does not currently extract
        # accounts_receivable, sga_expense, or gross PP&E for any filer.
        fundamentals = _fundamentals({"revenue": _quarterly_history("revenue", "Revenues", [(date(2026, 6, 30), 100.0)])})
        result = vm.beneish_m_score(fundamentals)
        self.assertEqual(result.status, vm.STATUS_SKIPPED)
        self.assertIn("accounts_receivable", result.skip_reason)
        self.assertIn("sga_expense", result.skip_reason)
        self.assertIn("gross_ppe", result.skip_reason)

    def test_hand_computed_when_all_inputs_present(self) -> None:
        base = date(2026, 4, 30)
        fundamentals = _fundamentals({
            "revenue": _ttm_quarters("revenue", "Revenues", base, current_total=110.0, prior_total=100.0),
            "gross_profit": _ttm_quarters("gross_profit", "GrossProfit", base, current_total=44.0, prior_total=40.0),
            "sga_expense": _ttm_quarters("sga_expense", "SellingGeneralAndAdministrativeExpense", base, current_total=15.0, prior_total=12.0),
            "depreciation_amortization": _ttm_quarters("depreciation_amortization", "DepreciationAndAmortization", base, current_total=30.0, prior_total=28.0),
            "net_income": _ttm_quarters("net_income", "NetIncomeLoss", base, current_total=40.0, prior_total=35.0),
            "operating_cashflow": _ttm_quarters("operating_cashflow", "NetCashProvidedByUsedInOperatingActivities", base, current_total=50.0, prior_total=45.0),
            "accounts_receivable": _two_point_instant("accounts_receivable", "AccountsReceivableNetCurrent", base, current=22.0, prior=20.0),
            "gross_ppe": _two_point_instant("gross_ppe", "PropertyPlantAndEquipmentGross", base, current=300.0, prior=280.0),
            "total_assets": _two_point_instant("total_assets", "Assets", base, current=700.0, prior=620.0),
            "total_current_assets": _two_point_instant("total_current_assets", "AssetsCurrent", base, current=200.0, prior=180.0),
            "total_current_liabilities": _two_point_instant("total_current_liabilities", "LiabilitiesCurrent", base, current=100.0, prior=90.0),
            "long_term_debt": _two_point_instant("long_term_debt", "LongTermDebt", base, current=50.0, prior=45.0),
            "short_term_investments": _two_point_instant("short_term_investments", "ShortTermInvestments", base, current=50.0, prior=40.0),
        })
        result = vm.beneish_m_score(fundamentals)
        self.assertEqual(result.status, vm.STATUS_OK, result.skip_reason)

        # Re-derive the expected M-score independently from the published formula,
        # using the same raw inputs as the fixture above.
        rec_c, rec_p, rev_c, rev_p = 22.0, 20.0, 110.0, 100.0
        gp_c, gp_p = 44.0, 40.0
        ca_c, ca_p, ppe_c, ppe_p, sec_c, sec_p, ta_c, ta_p = 200.0, 180.0, 300.0, 280.0, 50.0, 40.0, 700.0, 620.0
        da_c, da_p = 30.0, 28.0
        sga_c, sga_p = 15.0, 12.0
        cl_c, cl_p, ltd_c, ltd_p = 100.0, 90.0, 50.0, 45.0
        ni_c, ni_p, ocf_c, ocf_p = 40.0, 35.0, 50.0, 45.0

        dsri = (rec_c / rev_c) / (rec_p / rev_p)
        gmi = (gp_p / rev_p) / (gp_c / rev_c)
        aqi = (1 - (ca_c + ppe_c + sec_c) / ta_c) / (1 - (ca_p + ppe_p + sec_p) / ta_p)
        sgi = rev_c / rev_p
        depi = (da_p / (ppe_p + da_p)) / (da_c / (ppe_c + da_c))
        sgai = (sga_c / rev_c) / (sga_p / rev_p)
        lvgi = ((cl_c + ltd_c) / ta_c) / ((cl_p + ltd_p) / ta_p)
        tata = (ni_c - ocf_c) / ta_c
        expected_m = -4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi + 0.115 * depi - 0.172 * sgai - 0.327 * lvgi + 4.679 * tata

        self.assertAlmostEqual(result.outputs["m_score"], expected_m, places=6)
        self.assertAlmostEqual(result.outputs["dsri"], dsri, places=6)
        self.assertAlmostEqual(result.outputs["gmi"], gmi, places=6)


class ThreeScenarioExpectedValueTests(TestCase):
    def test_hand_computed_probability_weighting(self) -> None:
        derived = _derived(ttm_fcf=100.0, net_cash=0.0, shares_used=10.0, market_cap=1000.0)
        bear = ScenarioParams(fcf_growth_rate=0.0, discount_rate=0.10, terminal_growth=0.02, forecast_years=1)
        base = ScenarioParams(fcf_growth_rate=0.05, discount_rate=0.10, terminal_growth=0.02, forecast_years=1)
        bull = ScenarioParams(fcf_growth_rate=0.10, discount_rate=0.10, terminal_growth=0.02, forecast_years=1)
        result = vm.three_scenario_expected_value(derived, bear=bear, base=base, bull=bull, probabilities=(0.2, 0.5, 0.3))
        self.assertEqual(result.status, vm.STATUS_OK)

        bear_v = result.outputs["scenarios"]["bear"]["per_share_value"]
        base_v = result.outputs["scenarios"]["base"]["per_share_value"]
        bull_v = result.outputs["scenarios"]["bull"]["per_share_value"]
        expected_weighted = 0.2 * bear_v + 0.5 * base_v + 0.3 * bull_v
        self.assertAlmostEqual(result.outputs["probability_weighted_value"], expected_weighted, places=6)
        self.assertAlmostEqual(result.outputs["current_price"], 100.0, places=6)

    def test_skips_when_probabilities_do_not_sum_to_one(self) -> None:
        derived = _derived(ttm_fcf=100.0, net_cash=0.0, shares_used=10.0)
        result = vm.three_scenario_expected_value(derived, probabilities=(0.2, 0.2, 0.2))
        self.assertEqual(result.status, vm.STATUS_SKIPPED)
        self.assertIn("sum to 1.0", result.skip_reason)

    def test_skips_when_any_scenario_unresolvable(self) -> None:
        derived = _derived(ttm_fcf=-10.0, net_cash=0.0, shares_used=10.0)
        result = vm.three_scenario_expected_value(derived)
        self.assertEqual(result.status, vm.STATUS_SKIPPED)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class RouterTests(TestCase):
    def test_positive_stable_fcf_enables_dcf_variants(self) -> None:
        security = _security("ZM", Layer.AI_APPLICATIONS)
        derived = _derived(
            ttm_fcf=100.0, enterprise_value=1000.0, market_cap=1200.0, net_cash=200.0,
            non_operating_assets=10.0, shares_used=10.0,
            beta=BetaEstimate(beta=None, observations=0, r_squared=None, reliable=False, reason="no data"),
        )
        decision = vm.route(security, derived, None)
        for name in ("reverse_dcf", "two_stage_dcf", "owner_earnings"):
            self.assertIn(name, decision.enabled_models)
        self.assertEqual(decision.profile.fcf_sign, "positive")

    def test_negative_fcf_disables_all_dcf_variants_and_enables_cash_runway(self) -> None:
        security = _security("ZM", Layer.AI_APPLICATIONS)
        derived = _derived(ttm_fcf=-100.0, enterprise_value=1000.0, market_cap=1200.0, net_cash=200.0, shares_used=10.0)
        decision = vm.route(security, derived, None)
        excluded_names = {e["model"] for e in decision.excluded}
        for name in ("reverse_dcf", "two_stage_dcf", "owner_earnings"):
            self.assertIn(name, excluded_names)
            self.assertNotIn(name, decision.enabled_models)
        self.assertIn("cash_runway", decision.enabled_models)
        # A DCF on a cash-burning company is self-deception — the reason must say so.
        reasons = {e["model"]: e["reason"] for e in decision.excluded}
        self.assertIn("self-deception", reasons["reverse_dcf"])

    def test_negative_fcf_restricts_relative_valuation_multiples_to_ev_sales(self) -> None:
        security = _security("ZM", Layer.AI_APPLICATIONS)
        derived = _derived(
            ttm_fcf=-100.0, enterprise_value=1000.0, market_cap=1200.0, net_cash=200.0, shares_used=10.0,
            ttm_revenue=500.0,
        )
        decision = vm.route(security, derived, None)
        self.assertTrue(any("ev_sales" in note and "restricted" in note for note in decision.profile.notes))

        # Confirm the restriction actually reaches the model call, not just a note:
        # own_history_percentile with a fixture that CAN resolve should only report ev_sales.
        base = date(2026, 4, 30)
        dates = [base - timedelta(days=91 * i) for i in range(8)]
        revenue = _quarterly_history("revenue", "Revenues", [(d, 100.0) for d in dates])
        cash = _quarterly_history("cash_and_equivalents", "CashAndCashEquivalentsAtCarryingValue", [(d, 0.0) for d in dates[:5]])
        shares = _quarterly_history("diluted_weighted_avg_shares", "WeightedAverageNumberOfDilutedSharesOutstanding", [(d, 10.0) for d in dates[:5]])
        fundamentals = _fundamentals({"revenue": revenue, "cash_and_equivalents": cash, "diluted_weighted_avg_shares": shares})
        prices = _bars("TEST", [(d, 100.0 + i) for i, d in enumerate(dates[:5])])
        full_derived = _derived(
            ttm_fcf=-100.0, enterprise_value=1020.0, market_cap=1020.0, ttm_revenue=400.0, net_cash=200.0, shares_used=10.0,
        )
        report = vm.evaluate(security, full_derived, fundamentals, prices)
        own_history = next(r for r in report.results if r.model == "own_history_percentile")
        self.assertEqual(own_history.status, vm.STATUS_OK, own_history.skip_reason)
        self.assertEqual(own_history.assumptions.get("multiples_requested"), ["ev_sales"])
        self.assertEqual(set(own_history.outputs["multiples"].keys()), {"ev_sales"})

    def test_high_net_cash_ratio_forces_sotp(self) -> None:
        security = _security("ZM", Layer.AI_APPLICATIONS)
        # net_cash / market_cap = 500/1000 = 50% > 30% threshold
        derived = _derived(ttm_fcf=100.0, enterprise_value=500.0, market_cap=1000.0, net_cash=500.0, shares_used=10.0)
        decision = vm.route(security, derived, None)
        self.assertGreater(decision.profile.net_cash_to_market_cap, 0.30)
        self.assertIn("sum_of_the_parts", decision.enabled_models)
        self.assertTrue(any("FORCED" in note for note in decision.profile.notes))

    def test_material_non_operating_assets_forces_sotp(self) -> None:
        security = _security("ZM", Layer.AI_APPLICATIONS)
        derived = _derived(
            ttm_fcf=100.0, enterprise_value=900.0, market_cap=1000.0, net_cash=100.0,
            non_operating_assets=100.0,  # 10% of market cap > 5% materiality threshold
            shares_used=10.0,
        )
        decision = vm.route(security, derived, None)
        self.assertGreater(decision.profile.non_operating_to_market_cap, 0.05)
        self.assertIn("sum_of_the_parts", decision.enabled_models)
        self.assertTrue(any("FORCED" in note for note in decision.profile.notes))

    def test_unreliable_beta_uses_sector_default_range_not_capm(self) -> None:
        security = _security("ZM", Layer.AI_APPLICATIONS)
        unreliable_beta = BetaEstimate(beta=0.41, observations=125, r_squared=0.037, reliable=False, reason="unreliable")
        derived = _derived(ttm_fcf=100.0, enterprise_value=1000.0, beta=unreliable_beta)
        decision = vm.route(security, derived, None)
        self.assertEqual(decision.profile.discount_rate_method, "sector_default_range")
        self.assertEqual(decision.profile.discount_rates, RouterParams().sector_default_discount_rates["ai_applications"])
        self.assertGreater(len(decision.profile.discount_rates), 1)  # never a single point

    def test_reliable_beta_derives_capm_range_not_a_single_point(self) -> None:
        security = _security("ZM", Layer.AI_APPLICATIONS)
        reliable_beta = BetaEstimate(beta=1.2, observations=250, r_squared=0.45, reliable=True, reason=None)
        derived = _derived(ttm_fcf=100.0, enterprise_value=1000.0, beta=reliable_beta)
        decision = vm.route(security, derived, None)
        self.assertEqual(decision.profile.discount_rate_method, "capm_derived_range")
        self.assertGreater(len(decision.profile.discount_rates), 1)  # still a range, not a point

    def test_missing_altman_inputs_produce_named_skip_not_router_exclusion(self) -> None:
        security = _security("ZM", Layer.AI_APPLICATIONS)
        derived = _derived(ttm_fcf=100.0, enterprise_value=1000.0, market_cap=1200.0, net_cash=200.0, shares_used=10.0)
        report = vm.evaluate(security, derived, _fundamentals({}), [])
        altman = next(r for r in report.results if r.model == "altman_z_score")
        self.assertEqual(altman.status, vm.STATUS_SKIPPED)
        self.assertIn("total_assets", altman.skip_reason)

    def test_every_model_appears_exactly_once_in_the_report(self) -> None:
        security = _security("ZM", Layer.AI_APPLICATIONS)
        derived = _derived(ttm_fcf=100.0, enterprise_value=1000.0, market_cap=1200.0, net_cash=200.0, shares_used=10.0)
        report = vm.evaluate(security, derived, None, [])
        names = [r.model for r in report.results]
        self.assertEqual(sorted(names), sorted(vm.MODEL_NAMES))
        self.assertEqual(len(names), len(set(names)))

    def test_router_excluded_models_have_reason_prefixed(self) -> None:
        security = _security("ZM", Layer.AI_APPLICATIONS)
        derived = _derived(ttm_fcf=-10.0, enterprise_value=1000.0, market_cap=1200.0, net_cash=200.0, shares_used=10.0)
        report = vm.evaluate(security, derived, None, [])
        reverse = next(r for r in report.results if r.model == "reverse_dcf")
        self.assertEqual(reverse.status, vm.STATUS_SKIPPED)
        self.assertTrue(reverse.skip_reason.startswith("[router]"))


# ---------------------------------------------------------------------------
# Architecture / layer-boundary guarantees
# ---------------------------------------------------------------------------

class LayerBoundaryTests(TestCase):
    def test_report_carries_the_layer2_marker(self) -> None:
        security = _security("ZM", Layer.AI_APPLICATIONS)
        derived = _derived(ttm_fcf=100.0, enterprise_value=1000.0, market_cap=1200.0, net_cash=200.0, shares_used=10.0)
        report = vm.evaluate(security, derived, None, [])
        self.assertEqual(report.layer, vm.LAYER_MARKER)
        self.assertIn("advisory", report.layer)

    def test_module_does_not_import_scoring_or_pipeline_or_review_packets(self) -> None:
        import market_sentiment.valuation_models as package

        forbidden = {"market_sentiment.scoring", "market_sentiment.pipeline", "market_sentiment.review_packets"}
        for module_name, module in list(__import__("sys").modules.items()):
            if module_name.startswith("market_sentiment.valuation_models") and module is not None:
                imported = getattr(module, "__dict__", {})
                for value in imported.values():
                    module_of_value = getattr(value, "__module__", None)
                    if module_of_value in forbidden:
                        self.fail(f"{module_name} pulled in something from {module_of_value}")

    def test_to_dict_is_json_safe(self) -> None:
        import json

        security = _security("ZM", Layer.AI_APPLICATIONS)
        derived = _derived(ttm_fcf=100.0, enterprise_value=1000.0, market_cap=1200.0, net_cash=200.0, shares_used=10.0)
        report = vm.evaluate(security, derived, None, [])
        json.dumps(report.to_dict())  # must not raise
