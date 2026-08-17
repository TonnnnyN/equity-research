"""Tests for equity_research.valuation — derived valuation inputs (Layer 2 evidence only).

No live network involved: everything here is pure computation over in-memory
ValuationFundamentals / PriceBar fixtures.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from equity_research.models import (
    ConceptDatapoint,
    ConceptHistory,
    PriceBar,
    ShareClassEntry,
    ValuationFundamentals,
)
from equity_research.valuation import compute_beta, compute_valuation_derived


def _history(concept: str, tag: str, values: list[tuple[date, float]], unit: str = "USD") -> ConceptHistory:
    datapoints = [
        ConceptDatapoint(end=end, filed=end, value=value, form="10-Q")
        for end, value in values
    ]
    return ConceptHistory(concept=concept, tag=tag, unit=unit, datapoints=datapoints)


def _bars(ticker: str, closes: list[float], start: date) -> list[PriceBar]:
    return [
        PriceBar(
            ticker=ticker,
            trading_date=start + timedelta(days=index),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1000,
            source="stub",
        )
        for index, close in enumerate(closes)
    ]


class TtmComputationTests(TestCase):
    def test_sums_only_the_four_most_recent_quarters(self) -> None:
        fundamentals = ValuationFundamentals(
            ticker="ZM",
            cik="0001585521",
            source="sec_companyfacts",
            source_url=None,
            ingested_at=None,
            concepts={
                "revenue": _history(
                    "revenue",
                    "Revenues",
                    [
                        (date(2026, 1, 31), 100.0),
                        (date(2025, 10, 31), 95.0),
                        (date(2025, 7, 31), 90.0),
                        (date(2025, 4, 30), 85.0),
                        (date(2025, 1, 31), 80.0),  # 5th quarter — must be excluded from TTM
                    ],
                ),
            },
        )
        derived = compute_valuation_derived("ZM", date(2026, 3, 1), fundamentals, [], [])

        self.assertEqual(derived.ttm_revenue.value, 100.0 + 95.0 + 90.0 + 85.0)
        self.assertEqual(len(derived.ttm_revenue.inputs), 4)
        self.assertNotIn(date(2025, 1, 31).isoformat(), derived.ttm_revenue.inputs)

    def test_noncontiguous_quarters_return_none_with_reason_not_a_silent_sum(self) -> None:
        """If the 4 most recent quarterly datapoints handed to _concept_ttm are still not
        contiguous (e.g. sources/sec.py's quarter derivation could not bridge a gap), the
        TTM must come back as None with a reason — never silently summed as if the 4
        points were a real trailing-twelve-months. This is deliberately stricter than the
        old contract (which used to sum non-contiguous quarters anyway): a wrong-looking-
        plausible number is worse than an honest gap."""
        fundamentals = ValuationFundamentals(
            ticker="ZM", cik="1", source="sec_companyfacts", source_url=None, ingested_at=None,
            concepts={
                "revenue": _history(
                    "revenue", "RevenueFromContractWithCustomerExcludingAssessedTax",
                    [
                        (date(2026, 4, 30), 1239.0),
                        (date(2025, 10, 31), 1229.8),
                        (date(2025, 7, 31), 1217.2),
                        (date(2025, 4, 30), 1174.7),  # >100 days before 2025-07-31 -> gap flag
                    ],
                ),
            },
        )
        derived = compute_valuation_derived("ZM", date(2026, 8, 1), fundamentals, [], [])

        self.assertIsNone(derived.ttm_revenue.value)
        self.assertIsNotNone(derived.ttm_revenue.reason)
        self.assertIn("not contiguous", derived.ttm_revenue.reason)
        self.assertTrue(any("not contiguous" in gap for gap in derived.data_gaps))

    def test_fewer_than_four_quarters_yields_none_with_reason_and_data_gap(self) -> None:
        fundamentals = ValuationFundamentals(
            ticker="ZM",
            cik="0001585521",
            source="sec_companyfacts",
            source_url=None,
            ingested_at=None,
            concepts={
                "revenue": _history(
                    "revenue", "Revenues",
                    [(date(2026, 1, 31), 100.0), (date(2025, 10, 31), 95.0)],
                ),
            },
        )
        derived = compute_valuation_derived("ZM", date(2026, 3, 1), fundamentals, [], [])

        self.assertIsNone(derived.ttm_revenue.value)
        self.assertIsNotNone(derived.ttm_revenue.reason)
        self.assertTrue(any("revenue" in gap for gap in derived.data_gaps))


class MarketableSecuritiesAggregationTests(TestCase):
    def test_total_liquid_assets_sums_cash_plus_current_investments_only(self) -> None:
        """Reproduces the Zoom cash-is-not-cash scenario: cash alone is $890.9M but total
        liquid assets (cash + CURRENT marketable securities) is ~$4.1B here. Noncurrent
        ("long_term_investments") must NOT be folded into total_liquid_assets — the
        us-gaap tag for it is ambiguous across filers and can be a strategic-stake bucket
        rather than a liquid balance (see non_operating_assets tests below)."""
        fundamentals = ValuationFundamentals(
            ticker="ZM",
            cik="0001585521",
            source="sec_companyfacts",
            source_url=None,
            ingested_at=None,
            concepts={
                "cash_and_equivalents": _history(
                    "cash_and_equivalents", "CashAndCashEquivalentsAtCarryingValue",
                    [(date(2026, 1, 31), 890.9)],
                ),
                "short_term_investments": _history(
                    "short_term_investments", "MarketableSecuritiesCurrent",
                    [(date(2026, 1, 31), 3200.0)],
                ),
                "long_term_investments": _history(
                    "long_term_investments", "MarketableSecuritiesNoncurrent",
                    [(date(2026, 1, 31), 3609.1)],
                ),
            },
        )
        derived = compute_valuation_derived("ZM", date(2026, 3, 1), fundamentals, [], [])

        self.assertAlmostEqual(derived.total_liquid_assets.value, 890.9 + 3200.0)
        self.assertNotAlmostEqual(derived.total_liquid_assets.value, 890.9 + 3200.0 + 3609.1)
        self.assertAlmostEqual(derived.cash_and_equivalents.value, 890.9)
        # long_term_investments is still tracked (informational) but excluded from the sum.
        self.assertAlmostEqual(derived.long_term_investments.value, 3609.1)

    def test_zoom_total_liquid_assets_matches_verified_sec_figure(self) -> None:
        """Exact regression for the numbers verified by hand against Zoom's real SEC
        filings (CIK 0001585521): cash $0.891B + current marketable securities $6.830B
        (tagged AvailableForSaleSecuritiesDebtSecuritiesCurrent, which Zoom actually
        uses and the old tag list did not even look for) = $7.721B total liquid assets.
        LongTermInvestments ($1.876B, Zoom's strategic/venture stake bucket including its
        Anthropic position) must show up under non_operating_assets instead, and must not
        inflate total_liquid_assets."""
        fundamentals = ValuationFundamentals(
            ticker="ZM",
            cik="0001585521",
            source="sec_companyfacts",
            source_url=None,
            ingested_at=None,
            concepts={
                "cash_and_equivalents": _history(
                    "cash_and_equivalents", "CashAndCashEquivalentsAtCarryingValue",
                    [(date(2026, 4, 30), 891.0)],
                ),
                "short_term_investments": _history(
                    "short_term_investments", "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                    [(date(2026, 4, 30), 6830.0)],
                ),
                "non_operating_assets": _history(
                    "non_operating_assets", "LongTermInvestments",
                    [(date(2026, 4, 30), 1876.0)],
                ),
            },
        )
        derived = compute_valuation_derived("ZM", date(2026, 6, 1), fundamentals, [], [])

        self.assertAlmostEqual(derived.total_liquid_assets.value, 7721.0)
        self.assertAlmostEqual(derived.non_operating_assets.value, 1876.0)
        self.assertEqual(derived.non_operating_assets.source, "sec_companyfacts:LongTermInvestments")
        # Strategic stakes must never leak into the liquid-assets figure.
        self.assertNotAlmostEqual(derived.total_liquid_assets.value, 7721.0 + 1876.0)

    def test_non_operating_assets_none_with_reason_when_filer_has_none(self) -> None:
        """A filer with no strategic-stake tags at all (e.g. Apple) must get
        non_operating_assets=None with a reason, not a silently-defaulted zero."""
        fundamentals = ValuationFundamentals(
            ticker="AAPL", cik="0000320193", source="sec_companyfacts", source_url=None, ingested_at=None,
            concepts={
                "cash_and_equivalents": _history(
                    "cash_and_equivalents", "CashAndCashEquivalentsAtCarryingValue",
                    [(date(2026, 3, 31), 30000.0)],
                ),
            },
        )
        derived = compute_valuation_derived("AAPL", date(2026, 5, 1), fundamentals, [], [])

        self.assertIsNone(derived.non_operating_assets.value)
        self.assertIsNotNone(derived.non_operating_assets.reason)

    def test_missing_investment_concepts_treated_as_zero_not_as_missing_total(self) -> None:
        """When a filer simply has no marketable securities line item, total_liquid_assets
        should still compute (as cash alone) rather than going None — only cash itself is
        a hard requirement."""
        fundamentals = ValuationFundamentals(
            ticker="SMALLCAP",
            cik="1",
            source="sec_companyfacts",
            source_url=None,
            ingested_at=None,
            concepts={
                "cash_and_equivalents": _history(
                    "cash_and_equivalents", "CashAndCashEquivalentsAtCarryingValue",
                    [(date(2026, 1, 31), 50.0)],
                ),
            },
        )
        derived = compute_valuation_derived("SMALLCAP", date(2026, 3, 1), fundamentals, [], [])

        self.assertAlmostEqual(derived.total_liquid_assets.value, 50.0)

    def test_missing_cash_makes_total_liquid_assets_none(self) -> None:
        fundamentals = ValuationFundamentals(
            ticker="NOCASH",
            cik="1",
            source="sec_companyfacts",
            source_url=None,
            ingested_at=None,
            concepts={},
        )
        derived = compute_valuation_derived("NOCASH", date(2026, 3, 1), fundamentals, [], [])

        self.assertIsNone(derived.total_liquid_assets.value)
        self.assertIsNotNone(derived.total_liquid_assets.reason)


class SharesAndMarketCapTests(TestCase):
    def test_prefers_diluted_weighted_average_shares(self) -> None:
        fundamentals = ValuationFundamentals(
            ticker="ZM",
            cik="0001585521",
            source="sec_companyfacts",
            source_url=None,
            ingested_at=None,
            concepts={
                "diluted_weighted_avg_shares": _history(
                    "diluted_weighted_avg_shares", "WeightedAverageNumberOfDilutedSharesOutstanding",
                    [(date(2026, 1, 31), 293200000.0)], unit="shares",
                ),
                "basic_weighted_avg_shares": _history(
                    "basic_weighted_avg_shares", "WeightedAverageNumberOfSharesOutstandingBasic",
                    [(date(2026, 1, 31), 264600000.0)], unit="shares",
                ),
            },
            cover_page_shares=264600000.0,
            cover_page_share_classes=[ShareClassEntry(value=264600000.0, as_of=date(2026, 1, 31))],
            cover_page_meta={"tag": "dei:EntityCommonStockSharesOutstanding", "class_count": 1, "accn": "x"},
        )
        prices = _bars("ZM", [105.96], date(2026, 2, 1))
        derived = compute_valuation_derived("ZM", date(2026, 3, 1), fundamentals, prices, [])

        self.assertAlmostEqual(derived.shares_used.value, 293200000.0)
        self.assertEqual(derived.shares_used.basis, "diluted_weighted_average_shares_latest_quarter")

    def test_falls_back_to_cover_page_shares_summed_across_classes(self) -> None:
        """No diluted weighted-average count available: cover-page shares (already
        summed across classes by the SEC extraction layer) must be used instead of
        silently understating share count."""
        fundamentals = ValuationFundamentals(
            ticker="ZM",
            cik="0001585521",
            source="sec_companyfacts",
            source_url=None,
            ingested_at=None,
            concepts={},
            cover_page_shares=293200000.0,
            cover_page_share_classes=[
                ShareClassEntry(value=253000000.0, as_of=date(2026, 3, 1)),
                ShareClassEntry(value=40200000.0, as_of=date(2026, 3, 1)),
            ],
            cover_page_meta={"tag": "dei:EntityCommonStockSharesOutstanding", "class_count": 2, "accn": "acc1"},
        )
        derived = compute_valuation_derived("ZM", date(2026, 3, 1), fundamentals, [], [])

        self.assertAlmostEqual(derived.shares_used.value, 293200000.0)
        self.assertEqual(derived.shares_used.basis, "cover_page_shares_summed_across_classes")

    def test_market_cap_uses_latest_close_times_shares_used(self) -> None:
        fundamentals = ValuationFundamentals(
            ticker="ZM", cik="1", source="sec_companyfacts", source_url=None, ingested_at=None,
            concepts={},
            cover_page_shares=293200000.0,
            cover_page_share_classes=[ShareClassEntry(value=293200000.0, as_of=date(2026, 2, 1))],
            cover_page_meta={"tag": "dei:EntityCommonStockSharesOutstanding", "class_count": 1, "accn": "x"},
        )
        prices = _bars("ZM", [100.0, 102.0, 105.96], date(2026, 2, 1))
        derived = compute_valuation_derived("ZM", date(2026, 3, 1), fundamentals, prices, [])

        self.assertAlmostEqual(derived.market_cap.value, 105.96 * 293200000.0)

    def test_market_cap_none_when_shares_missing(self) -> None:
        prices = _bars("ZM", [100.0], date(2026, 2, 1))
        derived = compute_valuation_derived("ZM", date(2026, 3, 1), None, prices, [])

        self.assertIsNone(derived.market_cap.value)
        self.assertIsNotNone(derived.market_cap.reason)


class BetaRegressionTests(TestCase):
    def test_beta_recovers_known_linear_relationship(self) -> None:
        """Security returns are exactly 1.5x benchmark returns by construction; beta must
        recover ~1.5 with R^2 close to 1.0."""
        start = date(2026, 1, 1)
        benchmark_closes = [100.0]
        security_closes = [50.0]
        daily_returns = [0.01, -0.02, 0.015, 0.005, -0.01, 0.02, -0.005, 0.01, 0.008, -0.012, 0.006, 0.011]
        for r in daily_returns:
            benchmark_closes.append(benchmark_closes[-1] * (1 + r))
            security_closes.append(security_closes[-1] * (1 + 1.5 * r))

        prices = _bars("ZM", security_closes, start)
        benchmark_prices = _bars("QQQ", benchmark_closes, start)

        beta = compute_beta(prices, benchmark_prices)

        self.assertIsNotNone(beta.beta)
        self.assertAlmostEqual(beta.beta, 1.5, places=4)
        self.assertAlmostEqual(beta.r_squared, 1.0, places=4)
        self.assertEqual(beta.observations, len(daily_returns))

    def test_beta_none_with_reason_when_too_few_observations(self) -> None:
        start = date(2026, 1, 1)
        prices = _bars("ZM", [100.0, 101.0, 99.0], start)
        benchmark_prices = _bars("QQQ", [200.0, 201.0, 199.0], start)

        beta = compute_beta(prices, benchmark_prices)

        self.assertIsNone(beta.beta)
        self.assertIsNotNone(beta.reason)
        self.assertIn("insufficient", beta.reason)

    def test_beta_feeds_into_data_gaps_when_missing(self) -> None:
        derived = compute_valuation_derived("ZM", date(2026, 3, 1), None, [], [])
        self.assertIsNone(derived.beta.beta)
        self.assertTrue(any("beta" in gap for gap in derived.data_gaps))

    def test_beta_aligns_by_trading_date_not_by_independently_computed_returns(self) -> None:
        """Regression for the real defect: beta=0.136, r_squared=0.0055, observations=69
        out of ~90 available bars. Root cause was computing each series' own daily
        returns independently (against its own previous bar) and only intersecting by
        date afterward — whenever the security and benchmark calendars diverge even
        briefly (a bar missing from one series but not the other, e.g. different fetch
        cutoffs or holiday-calendar mismatches between two independently-sourced price
        lanes), the two returns being paired for a given date end up covering different,
        non-matching day spans, corrupting the regression. The fix restricts to dates
        both series actually have a close for BEFORE taking any difference, and pairs
        consecutive dates from that shared list.

        Security returns are exactly 1.5x benchmark returns by construction (same as
        test_beta_recovers_known_linear_relationship), but here the two price lanes each
        have their OWN independent set of missing dates (mimicking two independently
        fetched sources), never coinciding. Against the old implementation this recovers
        beta far from 1.5 with degraded R^2 (empirically ~1.49 / R^2~0.76 for this exact
        fixture); the fix must recover beta close to 1.5 with R^2 close to 1.0."""
        import math

        start = date(2026, 1, 1)
        n_days = 40
        daily_returns = [0.012 * math.sin(i * 1.7) + 0.004 * math.cos(i * 0.9) for i in range(n_days)]

        benchmark_closes = [100.0]
        security_closes = [50.0]
        for r in daily_returns:
            benchmark_closes.append(benchmark_closes[-1] * (1 + r))
            security_closes.append(security_closes[-1] * (1 + 1.5 * r))

        all_security_bars = _bars("ZM", security_closes, start)
        all_benchmark_bars = _bars("QQQ", benchmark_closes, start)

        # Deterministic, hand-picked, non-overlapping missing indices for each series.
        security_missing = {3, 9, 14, 20, 27, 33}
        benchmark_missing = {5, 11, 17, 23, 29, 36}
        security_bars = [b for i, b in enumerate(all_security_bars) if i not in security_missing]
        benchmark_bars = [b for i, b in enumerate(all_benchmark_bars) if i not in benchmark_missing]

        beta = compute_beta(security_bars, benchmark_bars)

        self.assertIsNotNone(beta.beta)
        self.assertAlmostEqual(beta.beta, 1.5, delta=0.01)
        self.assertGreater(beta.r_squared, 0.99)

    def test_beta_reliable_flag_true_for_high_confidence_estimate(self) -> None:
        start = date(2026, 1, 1)
        import math

        n_days = 65  # >= 60 so the "rough estimate" advisory note does not also fire
        daily_returns = [0.012 * math.sin(i * 1.3) + 0.003 * math.cos(i * 0.6) for i in range(n_days)]
        benchmark_closes = [100.0]
        security_closes = [50.0]
        for r in daily_returns:
            benchmark_closes.append(benchmark_closes[-1] * (1 + r))
            security_closes.append(security_closes[-1] * (1 + 1.5 * r))

        prices = _bars("ZM", security_closes, start)
        benchmark_prices = _bars("QQQ", benchmark_closes, start)

        beta = compute_beta(prices, benchmark_prices)

        self.assertTrue(beta.reliable)
        self.assertIsNone(beta.reason)

    def test_beta_flags_unreliable_when_r_squared_is_low(self) -> None:
        """Enough observations (>=30) but the security barely co-moves with the
        benchmark: r_squared must land below the reliability threshold and the estimate
        must be flagged reliable=False with a reason, even though a numeric beta is still
        returned (never silently substituted or hidden)."""
        start = date(2026, 1, 1)
        n_days = 40
        benchmark_returns = [0.01 if i % 2 == 0 else -0.01 for i in range(n_days)]
        # Security moves almost randomly with respect to the benchmark (alternating
        # unrelated pattern), so the regression should explain very little variance.
        security_returns = [0.002 * ((i * 37) % 5 - 2) for i in range(n_days)]

        benchmark_closes = [100.0]
        security_closes = [50.0]
        for br, sr in zip(benchmark_returns, security_returns):
            benchmark_closes.append(benchmark_closes[-1] * (1 + br))
            security_closes.append(security_closes[-1] * (1 + sr))

        prices = _bars("ZM", security_closes, start)
        benchmark_prices = _bars("QQQ", benchmark_closes, start)

        beta = compute_beta(prices, benchmark_prices)

        self.assertIsNotNone(beta.beta)
        self.assertLess(beta.r_squared, 0.10)
        self.assertFalse(beta.reliable)
        self.assertIsNotNone(beta.reason)

    def test_beta_flags_unreliable_when_observations_below_threshold(self) -> None:
        """Fewer than 30 paired observations (but at least the hard minimum of 5): a beta
        is still computed and returned, but flagged unreliable due to sample size."""
        start = date(2026, 1, 1)
        daily_returns = [0.01, -0.02, 0.015, 0.005, -0.01, 0.02, -0.005, 0.01]
        benchmark_closes = [100.0]
        security_closes = [50.0]
        for r in daily_returns:
            benchmark_closes.append(benchmark_closes[-1] * (1 + r))
            security_closes.append(security_closes[-1] * (1 + 1.5 * r))

        prices = _bars("ZM", security_closes, start)
        benchmark_prices = _bars("QQQ", benchmark_closes, start)

        beta = compute_beta(prices, benchmark_prices)

        self.assertIsNotNone(beta.beta)
        self.assertLess(beta.observations, 30)
        self.assertGreaterEqual(beta.observations, 5)
        self.assertFalse(beta.reliable)
        self.assertIn("observations", beta.reason)


class GracefulDegradationTests(TestCase):
    def test_none_fundamentals_produces_none_values_with_reasons_not_an_exception(self) -> None:
        derived = compute_valuation_derived("ZM", date(2026, 3, 1), None, [], [])

        self.assertIsNone(derived.shares_used.value)
        self.assertIsNone(derived.market_cap.value)
        self.assertIsNone(derived.ttm_revenue.value)
        self.assertIsNone(derived.net_cash.value)
        self.assertIsNone(derived.enterprise_value.value)
        self.assertTrue(len(derived.data_gaps) > 0)
        # Every ProvenancedValue with value=None must carry a reason.
        self.assertIsNotNone(derived.shares_used.reason)
        self.assertIsNotNone(derived.market_cap.reason)

    def test_ttm_fcf_and_ebitda_chain_propagate_missing_component_reasons(self) -> None:
        fundamentals = ValuationFundamentals(
            ticker="ZM", cik="1", source="sec_companyfacts", source_url=None, ingested_at=None,
            concepts={
                "operating_cashflow": _history(
                    "operating_cashflow", "NetCashProvidedByUsedInOperatingActivities",
                    [
                        (date(2026, 1, 31), 100.0), (date(2025, 10, 31), 95.0),
                        (date(2025, 7, 31), 90.0), (date(2025, 4, 30), 85.0),
                    ],
                ),
                # capex missing entirely -> ttm_fcf must be None, not silently 0.
            },
        )
        derived = compute_valuation_derived("ZM", date(2026, 3, 1), fundamentals, [], [])

        self.assertIsNotNone(derived.ttm_operating_cashflow.value)
        self.assertIsNone(derived.ttm_capex.value)
        self.assertIsNone(derived.ttm_fcf.value)
        self.assertIsNotNone(derived.ttm_fcf.reason)


class StaleTagPropagationTests(TestCase):
    """Coverage for propagating sources/sec.py's stale-tag-fallback signal (recorded on
    ValuationFundamentals.data_gaps and baked into ConceptHistory.tag) into
    ValuationDerived, per the fix for the NVDA/TSLA/AAPL stale-tag defect: a reader of
    ValuationDerived alone (the review packets, the 12 valuation models) must be able to
    see that a number came from a long-abandoned SEC tag, not just a reader of
    ValuationFundamentals."""

    _STALE_TAG = (
        "MarketableSecuritiesCurrent [STALE FALLBACK: latest datapoint 2022-09-30, "
        "638d before anchor 2024-06-30; no candidate tag reported within 548d of the "
        "anchor period]"
    )
    _STALE_GAP = f"short_term_investments: stale tag fallback ({_STALE_TAG})"

    def test_extraction_gap_merges_into_derived_data_gaps_with_attribution(self) -> None:
        """ValuationFundamentals.data_gaps entries must show up on ValuationDerived.data_gaps,
        clearly prefixed as extraction-level so they read differently from ordinary
        derivation-level gaps (e.g. a missing price lane)."""
        fundamentals = ValuationFundamentals(
            ticker="XOM", cik="0000034088", source="sec_companyfacts", source_url=None, ingested_at=None,
            concepts={
                "cash_and_equivalents": _history(
                    "cash_and_equivalents", "CashAndCashEquivalentsAtCarryingValue",
                    [(date(2026, 6, 30), 5000.0)],
                ),
                "short_term_investments": _history(
                    "short_term_investments", self._STALE_TAG, [(date(2022, 9, 30), 1570.0)],
                ),
            },
            data_gaps=[self._STALE_GAP],
        )
        # No price bars at all -> also generates a derivation-level gap, so both kinds
        # are present simultaneously and must stay distinguishable.
        derived = compute_valuation_derived("XOM", date(2026, 8, 1), fundamentals, [], [])

        extraction_gaps = [g for g in derived.data_gaps if g.startswith("extraction: ")]
        derivation_gaps = [g for g in derived.data_gaps if g.startswith("derivation: ")]

        self.assertEqual(extraction_gaps, [f"extraction: {self._STALE_GAP}"])
        self.assertTrue(any("market_cap" in g for g in derivation_gaps))
        # MECE: every gap has exactly one of the two prefixes, and the sets don't overlap.
        self.assertEqual(len(extraction_gaps) + len(derivation_gaps), len(derived.data_gaps))
        self.assertFalse(any(g.startswith("extraction: ") and g.startswith("derivation: ") for g in derived.data_gaps))

    def test_stale_sourced_value_carries_caveat_on_the_provenanced_value_itself(self) -> None:
        """The staleness must travel WITH short_term_investments (and anything derived
        from it), not just sit in the far-away data_gaps list."""
        fundamentals = ValuationFundamentals(
            ticker="XOM", cik="0000034088", source="sec_companyfacts", source_url=None, ingested_at=None,
            concepts={
                "cash_and_equivalents": _history(
                    "cash_and_equivalents", "CashAndCashEquivalentsAtCarryingValue",
                    [(date(2026, 6, 30), 5000.0)],
                ),
                "short_term_investments": _history(
                    "short_term_investments", self._STALE_TAG, [(date(2022, 9, 30), 1570.0)],
                ),
            },
            data_gaps=[self._STALE_GAP],
        )
        derived = compute_valuation_derived("XOM", date(2026, 8, 1), fundamentals, [], [])

        self.assertAlmostEqual(derived.short_term_investments.value, 1570.0)
        self.assertIn(self._STALE_GAP, derived.short_term_investments.caveats)

        # total_liquid_assets is derived (via _sum_optional) from cash + short_term
        # investments, so the caveat must survive the combination step too.
        self.assertAlmostEqual(derived.total_liquid_assets.value, 5000.0 + 1570.0)
        self.assertIn(self._STALE_GAP, derived.total_liquid_assets.caveats)

        # cash_and_equivalents itself came from a fresh tag and must stay caveat-free.
        self.assertEqual(derived.cash_and_equivalents.caveats, [])

    def test_zm_clean_control_gains_no_staleness_entries_and_verified_numbers_unchanged(self) -> None:
        """ZM (CIK 0001585521) is the clean control: sec.py finds nothing stale for it, so
        propagating fundamentals.data_gaps must add zero entries to derived.data_gaps, and
        no ProvenancedValue may pick up a caveat. The verified real figures (total_liquid_assets
        $7.721B, ttm_revenue $4,933.1M, net_cash $7.721B) must not move because of this change."""
        fundamentals = ValuationFundamentals(
            ticker="ZM", cik="0001585521", source="sec_companyfacts", source_url=None, ingested_at=None,
            concepts={
                "cash_and_equivalents": _history(
                    "cash_and_equivalents", "CashAndCashEquivalentsAtCarryingValue",
                    [(date(2026, 4, 30), 891.0)],
                ),
                "short_term_investments": _history(
                    "short_term_investments", "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                    [(date(2026, 4, 30), 6830.0)],
                ),
                "revenue": _history(
                    "revenue", "Revenues",
                    [
                        (date(2026, 4, 30), 1239.0),
                        (date(2026, 1, 31), 1229.8),
                        (date(2025, 10, 31), 1217.9),
                        (date(2025, 7, 31), 1246.4),
                    ],
                ),
            },
            data_gaps=[],  # clean control: sec.py recorded no staleness at all
        )
        derived = compute_valuation_derived("ZM", date(2026, 6, 1), fundamentals, [], [])

        self.assertEqual([g for g in derived.data_gaps if g.startswith("extraction: ")], [])
        self.assertEqual(derived.cash_and_equivalents.caveats, [])
        self.assertEqual(derived.short_term_investments.caveats, [])
        self.assertEqual(derived.total_liquid_assets.caveats, [])
        self.assertEqual(derived.ttm_revenue.caveats, [])

        self.assertAlmostEqual(derived.total_liquid_assets.value, 7721.0)
        self.assertAlmostEqual(derived.net_cash.value, 7721.0)
        self.assertAlmostEqual(derived.ttm_revenue.value, 4933.1, places=1)

    def test_derivation_only_gaps_never_get_extraction_prefix(self) -> None:
        """With no fundamentals at all, every gap is derivation-level (missing inputs
        computed inside this module) — none should ever be mistakenly attributed as
        coming from the extraction layer."""
        derived = compute_valuation_derived("NOFUNDAMENTALS", date(2026, 3, 1), None, [], [])

        self.assertTrue(len(derived.data_gaps) > 0)
        self.assertTrue(all(g.startswith("derivation: ") for g in derived.data_gaps))
        self.assertFalse(any(g.startswith("extraction: ") for g in derived.data_gaps))

    def test_multiple_stale_concepts_each_attributed_separately_no_cross_contamination(self) -> None:
        """XOM hits stale fallbacks on two independent concepts (short_term_investments,
        diluted_weighted_avg_shares). Each ProvenancedValue must carry only the caveat(s)
        relevant to the concept(s) that actually fed it — short_term_investments must not
        pick up the shares caveat, and vice versa."""
        stale_shares_tag = (
            "WeightedAverageNumberOfDilutedSharesOutstanding [STALE FALLBACK: latest "
            "datapoint 2021-12-31, 1673d before anchor 2026-06-30; no candidate tag "
            "reported within 548d of the anchor period]"
        )
        stale_shares_gap = f"diluted_weighted_avg_shares: stale tag fallback ({stale_shares_tag})"
        fundamentals = ValuationFundamentals(
            ticker="XOM", cik="0000034088", source="sec_companyfacts", source_url=None, ingested_at=None,
            concepts={
                "cash_and_equivalents": _history(
                    "cash_and_equivalents", "CashAndCashEquivalentsAtCarryingValue",
                    [(date(2026, 6, 30), 5000.0)],
                ),
                "short_term_investments": _history(
                    "short_term_investments", self._STALE_TAG, [(date(2022, 9, 30), 1570.0)],
                ),
                "diluted_weighted_avg_shares": _history(
                    "diluted_weighted_avg_shares", stale_shares_tag,
                    [(date(2021, 12, 31), 4200.0)], unit="shares",
                ),
            },
            data_gaps=[self._STALE_GAP, stale_shares_gap],
        )
        derived = compute_valuation_derived("XOM", date(2026, 8, 1), fundamentals, [], [])

        self.assertIn(self._STALE_GAP, derived.short_term_investments.caveats)
        self.assertNotIn(stale_shares_gap, derived.short_term_investments.caveats)

        self.assertIn(stale_shares_gap, derived.shares_used.caveats)
        self.assertNotIn(self._STALE_GAP, derived.shares_used.caveats)

        self.assertEqual(derived.cash_and_equivalents.caveats, [])

        extraction_gaps = {g for g in derived.data_gaps if g.startswith("extraction: ")}
        self.assertEqual(
            extraction_gaps,
            {f"extraction: {self._STALE_GAP}", f"extraction: {stale_shares_gap}"},
        )
