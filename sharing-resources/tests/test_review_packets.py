from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from market_sentiment.models import (
    ActionState,
    BetaEstimate,
    BucketScore,
    EventTag,
    FundamentalSnapshot,
    Layer,
    MacroObservation,
    OfficialEvent,
    OptionContract,
    OptionSnapshot,
    PipelineContext,
    PriceBar,
    PriceWindow,
    ProvenancedValue,
    ScoreCard,
    Security,
    SocialPost,
    SocialReboundState,
    SocialSnapshot,
    SocialSourceSummary,
    SocialThemeSummary,
    SourceStatus,
    TriggerResult,
    ValuationDerived,
)
from market_sentiment.review_packets import build_review_packet
from market_sentiment.storage import Storage


def _pv(value: float | None, reason: str | None = None) -> ProvenancedValue:
    return ProvenancedValue(value=value, reason=reason)


def _minimal_valuation_derived(**overrides) -> ValuationDerived:
    """A ValuationDerived with every ProvenancedValue field defaulted to value=None,
    overridable by keyword with a raw number — mirrors test_valuation_models.py's
    ``_derived`` builder so review-packet tests don't need the full 12-model fixture
    machinery to get at least one model producing real STATUS_OK output."""
    base = {
        "ticker": "ZM",
        "as_of": date(2026, 8, 17),
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
        base[key] = value if (key in ("beta", "data_gaps") or isinstance(value, ProvenancedValue)) else _pv(value)
    return ValuationDerived(**base)


class ReviewPacketTests(TestCase):
    def test_build_review_packet_includes_option_summary(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            options_snapshot=OptionSnapshot(
                ticker="MSFT",
                run_date=date(2026, 3, 26),
                source="alpha_vantage_options",
                contract_count=2,
                call_contracts=1,
                put_contracts=1,
                total_call_volume=220,
                total_put_volume=120,
                total_call_open_interest=1400,
                total_put_open_interest=900,
                put_call_volume_ratio=120 / 220,
                put_call_open_interest_ratio=900 / 1400,
                implied_volatility_avg=0.325,
                nearest_expiration=date(2026, 4, 17),
                nearest_days_to_expiry=22,
                max_call_open_interest_strike=400.0,
                max_put_open_interest_strike=380.0,
                top_contracts=[
                    OptionContract(
                        contract_id="MSFT260417C00400000",
                        expiration=date(2026, 4, 17),
                        strike=400.0,
                        option_type="call",
                        volume=220,
                        open_interest=1400,
                        implied_volatility=0.31,
                        last_price=12.40,
                    )
                ],
            ),
            options_source_statuses=[SourceStatus(source="alpha_vantage_options", success=True, message="ok")],
        )
        scorecard = ScoreCard(
            run_date=date(2026, 3, 26),
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 13, 20, ["options_call_skew_constructive"]),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=73,
            state=ActionState.STARTER,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        self.assertIn("option_summary", packet)
        self.assertEqual(packet["option_summary"]["contract_count"], 2)
        self.assertEqual(packet["option_summary"]["top_contracts"][0]["contract_id"], "MSFT260417C00400000")

    def test_social_summary_exposes_concise_source_brief(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            social_snapshot=SocialSnapshot(
                ticker="MSFT",
                run_date=date(2026, 3, 26),
                recent_window_hours=72,
                baseline_days=14,
                provider_count=2,
                community_count=2,
                total_posts=12,
                informative_posts=10,
                recent_posts=6,
                baseline_posts=4,
                unique_authors=5,
                author_concentration=0.4,
                recent_stance=0.35,
                baseline_stance=0.05,
                delta=0.30,
                breadth=0.8,
                hard_negative_ratio=0.1,
                state=SocialReboundState.MILD_REBOUND,
                score=4,
                provider_post_counts={"reddit": 8, "x": 4},
                recent_provider_post_counts={"reddit": 4, "x": 2},
                source_summaries=[
                    SocialSourceSummary(
                        source="reddit",
                        total_posts=8,
                        informative_posts=7,
                        recent_posts=4,
                        baseline_posts=3,
                        unique_authors=3,
                        recent_stance=0.4,
                        baseline_stance=0.1,
                        delta=0.3,
                        hard_negative_ratio=0.0,
                        top_bullish_themes=[SocialThemeSummary(label="demand", direction="positive", count=2)],
                        top_bearish_themes=[],
                        representative_posts=[
                            SocialPost(
                                ticker="MSFT",
                                source="reddit",
                                community="stocks",
                                post_id="r1",
                                created_at=datetime(2026, 3, 26, 10, 0, 0),
                                title="MSFT demand improving",
                                body="",
                                url="https://example.com/r1",
                            )
                        ],
                    ),
                    SocialSourceSummary(
                        source="x",
                        total_posts=4,
                        informative_posts=3,
                        recent_posts=2,
                        baseline_posts=1,
                        unique_authors=2,
                        recent_stance=-0.15,
                        baseline_stance=-0.05,
                        delta=-0.10,
                        hard_negative_ratio=0.5,
                        top_bullish_themes=[],
                        top_bearish_themes=[SocialThemeSummary(label="valuation", direction="negative", count=1)],
                        representative_posts=[
                            SocialPost(
                                ticker="MSFT",
                                source="x",
                                community="x",
                                post_id="x1",
                                created_at=datetime(2026, 3, 26, 11, 0, 0),
                                title="MSFT valuation debate",
                                body="",
                                url="https://example.com/x1",
                            )
                        ],
                    ),
                ],
            ),
        )
        scorecard = ScoreCard(
            run_date=date(2026, 3, 26),
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        source_brief = packet["social_summary"]["source_brief"]
        self.assertEqual(source_brief[0]["source"], "reddit")
        self.assertEqual(source_brief[0]["overall_tone"], "positive_improving")
        self.assertEqual(source_brief[1]["source"], "x")
        self.assertEqual(source_brief[1]["top_bearish_theme"], "valuation")

    def test_freshness_block_present_with_fresh_data(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 3, 26)
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[
                OfficialEvent(
                    ticker="MSFT",
                    event_time=datetime(2026, 3, 20),
                    form_type="10-Q",
                    title="MSFT filed quarterly results",
                    url="https://example.com",
                    source="sec",
                )
            ],
            fundamentals=FundamentalSnapshot(
                ticker="MSFT",
                cik="1",
                period_end=date(2026, 3, 10),  # 16 days before run_date
                filed_on=date(2026, 3, 15),
                revenue_latest=120,
                revenue_previous=100,
                operating_cashflow_latest=50,
                operating_cashflow_previous=40,
                capex_latest=10,
                cash_latest=90,
                debt_latest=40,
                source="sec_companyfacts",
            ),
            macro=[],
            source_statuses=[],
        )
        scorecard = ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        self.assertIn("freshness", packet)
        freshness = packet["freshness"]
        self.assertEqual(freshness["run_date"], "2026-03-26")
        self.assertEqual(freshness["fundamentals_period_end"], "2026-03-10")
        self.assertEqual(freshness["fundamentals_age_days"], 16)
        self.assertEqual(freshness["latest_official_event_date"], "2026-03-20")
        self.assertEqual(freshness["official_event_age_days"], 6)
        self.assertEqual(freshness["caveats"], [])

    def test_freshness_block_warns_on_stale_fundamentals(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 3, 26)
        stale_period_end = date(2025, 12, 15)  # 101 days before run_date
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=FundamentalSnapshot(
                ticker="MSFT",
                cik="1",
                period_end=stale_period_end,
                filed_on=date(2025, 12, 31),
                revenue_latest=120,
                revenue_previous=100,
                operating_cashflow_latest=50,
                operating_cashflow_previous=40,
                capex_latest=10,
                cash_latest=90,
                debt_latest=40,
                source="sec_companyfacts",
            ),
            macro=[],
            source_statuses=[],
        )
        scorecard = ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        freshness = packet["freshness"]
        self.assertEqual(freshness["fundamentals_age_days"], 101)
        self.assertTrue(any("current quarter likely unreported" in caveat for caveat in freshness["caveats"]))

    def test_freshness_block_handles_missing_fundamentals(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 3, 26)
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
        )
        scorecard = ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        freshness = packet["freshness"]
        self.assertIsNone(freshness["fundamentals_period_end"])
        self.assertIsNone(freshness["fundamentals_age_days"])
        self.assertTrue(any("no fundamentals snapshot available" in caveat for caveat in freshness["caveats"]))

    def test_freshness_block_handles_no_events(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 3, 26)
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=FundamentalSnapshot(
                ticker="MSFT",
                cik="1",
                period_end=date(2026, 3, 10),
                filed_on=date(2026, 3, 15),
                revenue_latest=120,
                revenue_previous=100,
                operating_cashflow_latest=50,
                operating_cashflow_previous=40,
                capex_latest=10,
                cash_latest=90,
                debt_latest=40,
                source="sec_companyfacts",
            ),
            macro=[],
            source_statuses=[],
        )
        scorecard = ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        freshness = packet["freshness"]
        self.assertIsNone(freshness["latest_official_event_date"])
        self.assertIsNone(freshness["official_event_age_days"])
        self.assertTrue(any("no official events available" in caveat for caveat in freshness["caveats"]))

    def test_earnings_calendar_block_with_data(self) -> None:
        """Test earnings_calendar block when context has fresh earnings data."""
        from market_sentiment.models import EarningsCalendar

        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 5, 15)
        earnings_date = date(2026, 7, 18)
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            earnings_calendar=EarningsCalendar(
                ticker="MSFT",
                next_earnings_date=earnings_date,
                is_estimate=False,
                fetched_at=datetime(2026, 5, 15, 10, 0, 0),
            ),
        )
        scorecard = ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 5, 15, 14, 0, 0), context, scorecard)

        earnings_calendar = packet["earnings_calendar"]
        self.assertEqual(earnings_calendar["status"], "ok")
        self.assertEqual(earnings_calendar["next_earnings_date"], "2026-07-18")
        self.assertEqual(earnings_calendar["days_to_next_earnings"], 64)
        self.assertFalse(earnings_calendar["is_estimate"])

    def test_earnings_calendar_block_unavailable(self) -> None:
        """Test earnings_calendar block when context.earnings_calendar exists but has no date."""
        from market_sentiment.models import EarningsCalendar

        security = Security(ticker="SMALL_CAP", name="Small Cap", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 5, 15)
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            earnings_calendar=EarningsCalendar(
                ticker="SMALL_CAP",
                next_earnings_date=None,
                is_estimate=False,
                fetched_at=datetime(2026, 5, 15, 10, 0, 0),
            ),
        )
        scorecard = ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 5, 15, 14, 0, 0), context, scorecard)

        earnings_calendar = packet["earnings_calendar"]
        self.assertEqual(earnings_calendar["status"], "unavailable")
        self.assertIsNone(earnings_calendar["next_earnings_date"])
        self.assertIsNone(earnings_calendar["days_to_next_earnings"])
        self.assertIsNone(earnings_calendar["is_estimate"])

    def test_earnings_calendar_block_missing(self) -> None:
        """Test earnings_calendar block when context.earnings_calendar is None."""
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 5, 15)
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            earnings_calendar=None,
        )
        scorecard = ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 5, 15, 14, 0, 0), context, scorecard)

        earnings_calendar = packet["earnings_calendar"]
        self.assertEqual(earnings_calendar["status"], "failed")
        self.assertIsNone(earnings_calendar["next_earnings_date"])
        self.assertIsNone(earnings_calendar["days_to_next_earnings"])
        self.assertIsNone(earnings_calendar["is_estimate"])

    # ------------------------------------------------------------------
    # Valuation MODEL layer wiring (valuation_models.evaluate -> valuation_inputs.models)
    # ------------------------------------------------------------------

    def _minimal_scorecard(self, security: Security, run_date: date) -> ScoreCard:
        return ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

    def test_valuation_inputs_includes_always_on_metrics(self) -> None:
        """Test that always-on metrics (piotroski, altman, beneish, net_cash_floor) are
        computed at packet-build time and exposed under valuation_inputs.always_on.
        The models key should be None (ordered results arrive later via CLI)."""
        security = Security(ticker="ZM", name="Zoom", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 8, 17)
        derived = _minimal_valuation_derived(
            shares_used=300_200_000,
            market_cap=31_810_000_000,
            net_cash=7_720_000_000,
            total_liquid_assets=7_720_000_000,
            non_operating_assets=1_880_000_000,
            enterprise_value=24_090_000_000,
            ttm_fcf=1_961_000_000,
        )
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            valuation_derived=derived,
        )
        scorecard = self._minimal_scorecard(security, run_date)

        packet = build_review_packet(datetime(2026, 8, 17, 14, 0, 0), context, scorecard)

        valuation_inputs = packet["valuation_inputs"]
        self.assertIn("always_on", valuation_inputs)
        self.assertIn("models", valuation_inputs)

        # Check always_on metrics are present
        always_on = valuation_inputs["always_on"]
        self.assertIsNotNone(always_on)
        always_on_model_names = {r["model"] for r in always_on}
        self.assertIn("net_cash_floor", always_on_model_names)

        # models should be None (ordered results don't exist at packet-build time)
        self.assertIsNone(valuation_inputs["models"])

        # Layer 2 advisory-only guarantees: must not leak into scoring.
        self.assertNotIn("models", packet["bucket_scores"])
        self.assertFalse(packet["rule_engine_precheck"]["partial_coverage"])

    def test_valuation_inputs_models_none_when_derived_missing(self) -> None:
        security = Security(ticker="ZM", name="Zoom", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 8, 17)
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            valuation_fundamentals=None,
            valuation_derived=None,
        )
        # Force the "fundamentals or derived present" branch without a usable derived
        # by attaching a bare, empty-ish fundamentals snapshot via a peer-comparison
        # style attribute — simplest is just to confirm valuation_inputs is None end
        # to end when neither is present (existing behavior preserved).
        packet = build_review_packet(datetime(2026, 8, 17, 14, 0, 0), context, self._minimal_scorecard(security, run_date))
        self.assertIsNone(packet["valuation_inputs"])

    def test_packet_generation_with_and_without_peers(self) -> None:
        """Test that packets are generated correctly whether or not peer contexts are provided.
        Since ordered models are placed via CLI (not at packet-build time), peer_contexts
        parameter is now unused at packet-build time but should still be accepted."""
        security = Security(ticker="ZM", name="Zoom", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        peer_security = Security(ticker="PEER", name="Peer Co", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 8, 17)

        subject_derived = _minimal_valuation_derived(
            market_cap=31_810_000_000,
            enterprise_value=24_090_000_000,
            ttm_fcf=1_961_000_000,
        )
        peer_derived = _minimal_valuation_derived(
            ticker="PEER",
            market_cap=50_000_000_000,
            enterprise_value=45_000_000_000,
            ttm_fcf=2_000_000_000,
        )

        peer_context = PipelineContext(
            security=peer_security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            valuation_derived=peer_derived,
        )
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            valuation_derived=subject_derived,
        )
        scorecard = self._minimal_scorecard(security, run_date)

        # Without peers: packet should still be generated, models should be None.
        packet_no_peers = build_review_packet(datetime(2026, 8, 17, 14, 0, 0), context, scorecard)
        self.assertIsNotNone(packet_no_peers["valuation_inputs"])
        self.assertIsNone(packet_no_peers["valuation_inputs"]["models"])
        self.assertIsNotNone(packet_no_peers["valuation_inputs"]["always_on"])

        # With peers: packet should still be generated, models still None (ordered later).
        packet_with_peers = build_review_packet(
            datetime(2026, 8, 17, 14, 0, 0), context, scorecard, peer_contexts=[context, peer_context]
        )
        self.assertIsNotNone(packet_with_peers["valuation_inputs"])
        self.assertIsNone(packet_with_peers["valuation_inputs"]["models"])
        self.assertIsNotNone(packet_with_peers["valuation_inputs"]["always_on"])

    def test_always_on_metrics_exception_degrades_gracefully(self) -> None:
        """Test that if always-on metrics fail to compute, they degrade to None rather than
        failing the packet. The packet must still be generated completely."""
        security = Security(ticker="ZM", name="Zoom", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 8, 17)
        derived = _minimal_valuation_derived(market_cap=31_810_000_000)
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            valuation_derived=derived,
        )
        scorecard = self._minimal_scorecard(security, run_date)

        # Simulate an exception during always-on metric computation
        with patch("market_sentiment.review_packets.valuation_models.piotroski_f_score", side_effect=RuntimeError("boom")):
            packet = build_review_packet(datetime(2026, 8, 17, 14, 0, 0), context, scorecard)

        # The whole packet must still be generated; always_on degrades to None.
        self.assertIsNotNone(packet)
        self.assertIsNone(packet["valuation_inputs"]["always_on"])
        self.assertIsNone(packet["valuation_inputs"]["models"])
        self.assertEqual(packet["rule_engine_precheck"]["state"], "Watch")
        self.assertFalse(packet["rule_engine_precheck"]["partial_coverage"])

