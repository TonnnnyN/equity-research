from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest import TestCase

from market_sentiment.models import (
    ActionState,
    EventTag,
    FundamentalSnapshot,
    Layer,
    OfficialEvent,
    OptionContract,
    OptionSnapshot,
    PriceBar,
    Security,
    SocialReboundState,
    SocialSnapshot,
    SourceStatus,
    TriggerResult,
)
from market_sentiment.pipeline import dedupe_statuses
from market_sentiment.scoring import (
    build_scorecard,
    cap_state_if_data_insufficient,
    score_price_flow,
    score_social_rebound,
)


class ScoringTests(TestCase):
    def test_negative_keyword_triggers_veto(self) -> None:
        security = Security(ticker="TEST", name="Test Co", layer=Layer.COMPUTE, benchmark="SOXX")
        event = OfficialEvent(
            ticker="TEST",
            event_time=datetime(2026, 3, 1),
            form_type="8-K",
            title="TEST filed bankruptcy notice",
            url="https://example.com",
            source="sec",
        )
        trigger = TriggerResult(triggered=True, reasons=["ten_day_drawdown"], ten_day_drawdown=0.2, twenty_day_drawdown=0.25, relative_underperformance=0.1, new_low=False)
        context = type(
            "Context",
            (),
            {
                "security": security,
                "benchmark_ticker": "SOXX",
                "prices": [],
                "benchmark_prices": [],
                "official_events": [event],
                "fundamentals": None,
                "macro": [],
                "source_statuses": [SourceStatus(source="sec", success=True)],
            },
        )()

        scorecard = build_scorecard(
            run_date=date(2026, 3, 26),
            context=context,
            trigger=trigger,
            event_tag=EventTag.COMPANY_SPECIFIC,
            peer_contexts=[],
        )

        self.assertEqual(scorecard.state, ActionState.REJECT)
        self.assertEqual(scorecard.veto_reason, "negative_official_keyword")

    def test_dedupe_statuses_collapses_duplicates(self) -> None:
        statuses = [
            SourceStatus(source="sec", success=True, message="ok", payload_path="a"),
            SourceStatus(source="sec", success=True, message="ok", payload_path="a"),
            SourceStatus(source="fred", success=False, partial=True, message="missing"),
        ]
        deduped = dedupe_statuses(statuses, datetime.now(timezone.utc))
        self.assertEqual(len(deduped), 2)

    def test_companyfacts_lifts_fundamental_score(self) -> None:
        security = Security(ticker="TEST", name="Test Co", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        snapshot = FundamentalSnapshot(
            ticker="TEST",
            cik="0000000001",
            period_end=date(2025, 12, 31),
            filed_on=date(2026, 2, 1),
            revenue_latest=120.0,
            revenue_previous=100.0,
            operating_cashflow_latest=40.0,
            operating_cashflow_previous=35.0,
            capex_latest=10.0,
            cash_latest=80.0,
            debt_latest=50.0,
            source="sec_companyfacts",
        )
        trigger = TriggerResult(triggered=True, reasons=["ten_day_drawdown"], ten_day_drawdown=0.2, twenty_day_drawdown=0.25, relative_underperformance=0.1, new_low=False)
        context = type(
            "Context",
            (),
            {
                "security": security,
                "benchmark_ticker": "QQQ",
                "prices": [],
                "benchmark_prices": [],
                "official_events": [],
                "fundamentals": snapshot,
                "macro": [],
                "source_statuses": [SourceStatus(source="sec_companyfacts", success=True)],
            },
        )()

        scorecard = build_scorecard(
            run_date=date(2026, 3, 26),
            context=context,
            trigger=trigger,
            event_tag=EventTag.COMPANY_SPECIFIC,
            peer_contexts=[],
        )
        self.assertGreaterEqual(scorecard.fundamentals.score, 20)

    def test_options_call_skew_supports_chain_confirmation(self) -> None:
        security = Security(ticker="TEST", name="Test Co", layer=Layer.COMPUTE, benchmark="SOXX")
        trigger = TriggerResult(triggered=True, reasons=["ten_day_drawdown"], ten_day_drawdown=0.2, twenty_day_drawdown=0.25, relative_underperformance=0.1, new_low=False)
        context = type(
            "Context",
            (),
            {
                "security": security,
                "benchmark_ticker": "SOXX",
                "prices": [],
                "benchmark_prices": [],
                "official_events": [],
                "fundamentals": None,
                "macro": [],
                "source_statuses": [SourceStatus(source="stooq", success=True)],
                "options_snapshot": OptionSnapshot(
                    ticker="TEST",
                    run_date=date(2026, 3, 26),
                    source="alpha_vantage_options",
                    contract_count=4,
                    call_contracts=2,
                    put_contracts=2,
                    total_call_volume=320,
                    total_put_volume=120,
                    total_call_open_interest=1200,
                    total_put_open_interest=700,
                    put_call_volume_ratio=0.38,
                    put_call_open_interest_ratio=0.58,
                    implied_volatility_avg=0.42,
                    nearest_expiration=date(2026, 4, 17),
                    nearest_days_to_expiry=22,
                    max_call_open_interest_strike=100.0,
                    max_put_open_interest_strike=90.0,
                    top_contracts=[
                        OptionContract(
                            contract_id="TEST260417C00100000",
                            expiration=date(2026, 4, 17),
                            strike=100.0,
                            option_type="call",
                            volume=180,
                            open_interest=650,
                            implied_volatility=0.38,
                            last_price=4.2,
                        )
                    ],
                ),
            },
        )()

        scorecard = build_scorecard(
            run_date=date(2026, 3, 26),
            context=context,
            trigger=trigger,
            event_tag=EventTag.COMPANY_SPECIFIC,
            peer_contexts=[],
        )

        self.assertGreaterEqual(scorecard.chain_confirmation.score, 19)
        self.assertIn("options_call_skew_constructive", scorecard.chain_confirmation.notes)

    def test_cap_state_caps_add_to_watch_when_sec_and_price_missing(self) -> None:
        source_health = [
            SourceStatus(source="sec", success=False, partial=False),
            SourceStatus(source="alpha_vantage", success=False, partial=False),
        ]
        result_state, insufficient = cap_state_if_data_insufficient(ActionState.ADD, source_health)
        self.assertEqual(result_state, ActionState.WATCH)
        self.assertTrue(insufficient)

    def test_cap_state_preserves_state_when_data_ok(self) -> None:
        source_health = [
            SourceStatus(source="sec", success=True, partial=False),
            SourceStatus(source="alpha_vantage", success=True, partial=False),
        ]
        result_state, insufficient = cap_state_if_data_insufficient(ActionState.ADD, source_health)
        self.assertEqual(result_state, ActionState.ADD)
        self.assertFalse(insufficient)

    def test_cap_state_treats_cached_prices_as_insufficient(self) -> None:
        source_health = [
            SourceStatus(source="sec", success=True, partial=False),
            SourceStatus(source="daily_prices_cache", success=True, partial=True),
        ]
        result_state, insufficient = cap_state_if_data_insufficient(ActionState.ADD, source_health)
        self.assertEqual(result_state, ActionState.WATCH)
        self.assertTrue(insufficient)

    def test_cap_state_not_insufficient_when_tiger_succeeds(self) -> None:
        source_health = [
            SourceStatus(source="sec", success=True, partial=False),
            SourceStatus(source="tiger", success=True, partial=False),
        ]
        result_state, insufficient = cap_state_if_data_insufficient(ActionState.STARTER, source_health)
        self.assertEqual(result_state, ActionState.STARTER)
        self.assertFalse(insufficient)

    def test_cap_state_not_insufficient_when_yahoo_succeeds(self) -> None:
        source_health = [
            SourceStatus(source="sec", success=True, partial=False),
            SourceStatus(source="yahoo_chart", success=True, partial=False),
        ]
        result_state, insufficient = cap_state_if_data_insufficient(ActionState.ADD, source_health)
        self.assertEqual(result_state, ActionState.ADD)
        self.assertFalse(insufficient)

    def test_cap_state_insufficient_when_only_cache_serves_price(self) -> None:
        source_health = [
            SourceStatus(source="sec", success=True, partial=False),
            SourceStatus(source="daily_prices_cache", success=True, partial=True),
        ]
        result_state, insufficient = cap_state_if_data_insufficient(ActionState.STARTER, source_health)
        self.assertEqual(result_state, ActionState.WATCH)
        self.assertTrue(insufficient)


def make_price_bars(ticker: str, closes: list[float], start_date: date | None = None) -> list[PriceBar]:
    """Helper to construct PriceBar lists for testing."""
    if start_date is None:
        start_date = date(2026, 1, 1)
    bars = []
    for index, close in enumerate(closes):
        bars.append(
            PriceBar(
                ticker=ticker,
                trading_date=start_date + timedelta(days=index),
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=1000.0 + index,
                source="test",
            )
        )
    return bars


class PriceFlowTests(TestCase):
    def test_price_flow_basing_stock_scores_high(self) -> None:
        """Stock drops sharply, then forms a constructive base with closes near highs."""
        # Drop from 100 to 85 in first 5 days, then base/recover with strong closes
        closes = [
            100, 98, 95, 90, 85,  # sharp drop (days 1-5)
            86, 87, 86, 88, 87,   # start recovering
            88, 89, 88, 90, 89,   # consolidate higher
            91, 92, 90, 93, 91,   # continue recovery
            94, 95, 93, 96, 95,   # reaches 95 (close to drop endpoint)
        ]
        bars = make_price_bars("TEST", closes)
        result = score_price_flow(bars)

        self.assertGreaterEqual(result.score, 10)
        self.assertEqual(result.max_score, 15)
        self.assertIn("basing_no_new_lows", result.notes)

    def test_price_flow_falling_knife_scores_low(self) -> None:
        """Stock in steady decline; closes near daily lows; below both SMAs."""
        # Steady downtrend, each close near the day's low
        closes = [
            100, 99, 98, 97, 96,
            95, 94, 93, 92, 91,
            90, 89, 88, 87, 86,
            85, 84, 83, 82, 81,
            80, 79, 78, 77, 76,
        ]
        bars = make_price_bars("TEST", closes)
        result = score_price_flow(bars)

        self.assertLessEqual(result.score, 4)
        self.assertEqual(result.max_score, 15)
        self.assertIn("still_making_lows", result.notes)

    def test_price_flow_insufficient_history(self) -> None:
        """Fewer than 21 bars should return score 0 with insufficient_price_history note."""
        closes = [100 - i for i in range(15)]  # Only 15 bars
        bars = make_price_bars("TEST", closes)
        result = score_price_flow(bars)

        self.assertEqual(result.score, 0)
        self.assertEqual(result.max_score, 15)
        self.assertIn("insufficient_price_history", result.notes)

    def test_price_flow_flat_day_neutral_close_location(self) -> None:
        """A bar with high == low should not raise ZeroDivisionError; treats as 0.5."""
        closes = [
            100, 99, 98, 97, 96,
            95, 94, 93, 92, 91,
            90, 89, 88, 87, 86,
            85, 84, 83, 82, 81,
            82, 83, 84, 85, 84,  # last 5 bars
        ]
        bars = make_price_bars("TEST", closes)
        # Modify the last bar to have high == low (flat day)
        bars[-1] = PriceBar(
            ticker="TEST",
            trading_date=bars[-1].trading_date,
            open=84.0,
            high=84.0,  # high == low
            low=84.0,
            close=84.0,
            volume=1000.0,
            source="test",
        )

        # Should not raise ZeroDivisionError
        result = score_price_flow(bars)
        self.assertIsNotNone(result)
        self.assertEqual(result.max_score, 15)

    def test_price_flow_score_bounded_0_to_15(self) -> None:
        """Score should always be between 0 and 15."""
        # Strongly constructive: stock stabilizes and recovers well
        closes = [
            100, 98, 95, 92, 88,  # drop 12%
            88, 89, 89, 90, 90,   # stabilize
            91, 92, 92, 93, 94,   # strong recovery
            95, 96, 97, 98, 99,   # continue up
            100, 101, 102, 101, 102,  # reach and exceed prior high
        ]
        bars = make_price_bars("TEST", closes)
        result = score_price_flow(bars)

        self.assertGreaterEqual(result.score, 0)
        self.assertLessEqual(result.score, 15)
        self.assertEqual(result.max_score, 15)


class SocialRebound(TestCase):
    def test_social_rebound_no_sample_sets_max_zero(self) -> None:
        """Empty social snapshot → bucket max == 0, note contains 'no_social_sample'."""
        result = score_social_rebound(snapshot=None, statuses=[], partial_coverage=False)

        self.assertEqual(result.score, 0)
        self.assertEqual(result.max_score, 0)
        self.assertIn("no_social_sample", result.notes)

    def test_social_rebound_insufficient_data_sets_max_zero(self) -> None:
        """Social sample present but insufficient_data state → max == 0, no_social_sample note."""
        snapshot = SocialSnapshot(
            ticker="TEST",
            run_date=date(2026, 3, 26),
            recent_window_hours=72,
            baseline_days=30,
            provider_count=3,
            community_count=0,
            total_posts=0,
            informative_posts=0,
            recent_posts=0,
            baseline_posts=0,
            unique_authors=0,
            author_concentration=0.0,
            recent_stance=0.0,
            baseline_stance=0.0,
            delta=0.0,
            breadth=0.0,
            hard_negative_ratio=0.0,
            state=SocialReboundState.INSUFFICIENT,
            score=0,
        )
        result = score_social_rebound(snapshot=snapshot, statuses=[], partial_coverage=False)

        self.assertEqual(result.score, 0)
        self.assertEqual(result.max_score, 0)
        self.assertIn("no_social_sample", result.notes)

    def test_social_rebound_neutral_sample_keeps_max_ten(self) -> None:
        """Social sample present but neutral → max == 10, score == 0."""
        snapshot = SocialSnapshot(
            ticker="TEST",
            run_date=date(2026, 3, 26),
            recent_window_hours=72,
            baseline_days=30,
            provider_count=3,
            community_count=5,
            total_posts=50,
            informative_posts=30,
            recent_posts=10,
            baseline_posts=15,
            unique_authors=8,
            author_concentration=0.2,
            recent_stance=0.0,
            baseline_stance=0.0,
            delta=0.0,
            breadth=0.5,
            hard_negative_ratio=0.1,
            state=SocialReboundState.FLAT,
            score=0,
        )
        result = score_social_rebound(snapshot=snapshot, statuses=[], partial_coverage=False)

        self.assertEqual(result.score, 0)
        self.assertEqual(result.max_score, 10)
        self.assertIn("social_flat_unclear", result.notes)
        self.assertNotIn("no_social_sample", result.notes)

    def test_social_rebound_positive_sample_keeps_max_ten(self) -> None:
        """Social sample with positive score → max == 10, score > 0."""
        snapshot = SocialSnapshot(
            ticker="TEST",
            run_date=date(2026, 3, 26),
            recent_window_hours=72,
            baseline_days=30,
            provider_count=3,
            community_count=5,
            total_posts=100,
            informative_posts=80,
            recent_posts=30,
            baseline_posts=15,
            unique_authors=15,
            author_concentration=0.15,
            recent_stance=0.7,
            baseline_stance=0.2,
            delta=0.5,
            breadth=0.8,
            hard_negative_ratio=0.05,
            state=SocialReboundState.STRONG_REBOUND,
            score=8,
        )
        result = score_social_rebound(snapshot=snapshot, statuses=[], partial_coverage=False)

        self.assertEqual(result.score, 8)
        self.assertEqual(result.max_score, 10)
        self.assertIn("social_strong_rebound", result.notes)

    def test_map_state_thresholds_scale_when_social_absent(self) -> None:
        """Two identical scorecards, one with social, one without; no-social not penalized."""
        security = Security(ticker="TEST", name="Test Co", layer=Layer.COMPUTE, benchmark="SOXX")
        trigger = TriggerResult(
            triggered=True,
            reasons=["ten_day_drawdown"],
            ten_day_drawdown=0.2,
            twenty_day_drawdown=0.25,
            relative_underperformance=0.1,
            new_low=False,
        )
        # Create context with 25 price bars (sufficient for price_flow scoring)
        prices = make_price_bars(
            "TEST",
            [100 - i * 0.5 for i in range(25)],  # gentle downtrend: 100, 99.5, 99, ..., 87.5
            start_date=date(2026, 2, 26),
        )
        context_base = type(
            "Context",
            (),
            {
                "security": security,
                "benchmark_ticker": "SOXX",
                "prices": prices,
                "benchmark_prices": [],
                "official_events": [],
                "fundamentals": None,
                "macro": [],
                "source_statuses": [SourceStatus(source="stooq", success=True)],
            },
        )()

        # Build scorecard without social
        scorecard_no_social = build_scorecard(
            run_date=date(2026, 3, 26),
            context=context_base,
            trigger=trigger,
            event_tag=EventTag.COMPANY_SPECIFIC,
            peer_contexts=[],
        )

        # Build scorecard with social (neutral sample, max=10)
        snapshot = SocialSnapshot(
            ticker="TEST",
            run_date=date(2026, 3, 26),
            recent_window_hours=72,
            baseline_days=30,
            provider_count=3,
            community_count=5,
            total_posts=50,
            informative_posts=30,
            recent_posts=10,
            baseline_posts=15,
            unique_authors=8,
            author_concentration=0.2,
            recent_stance=0.0,
            baseline_stance=0.0,
            delta=0.0,
            breadth=0.5,
            hard_negative_ratio=0.1,
            state=SocialReboundState.FLAT,
            score=0,
        )
        context_with_social = type(
            "Context",
            (),
            {
                "security": security,
                "benchmark_ticker": "SOXX",
                "prices": prices,
                "benchmark_prices": [],
                "official_events": [],
                "fundamentals": None,
                "macro": [],
                "source_statuses": [SourceStatus(source="stooq", success=True)],
                "social_snapshot": snapshot,
                "social_source_statuses": [SourceStatus(source="social_api", success=True)],
            },
        )()

        scorecard_with_social = build_scorecard(
            run_date=date(2026, 3, 26),
            context=context_with_social,
            trigger=trigger,
            event_tag=EventTag.COMPANY_SPECIFIC,
            peer_contexts=[],
        )

        # Both should map to the same state (threshold scaling means no-social is not penalized)
        # since the social score is 0 (neutral), adding it doesn't help, but achievable_max scales the threshold
        self.assertEqual(
            scorecard_no_social.state,
            scorecard_with_social.state,
            msg=f"no_social state={scorecard_no_social.state}, with_social state={scorecard_with_social.state}",
        )

    def test_existing_with_social_behavior_unchanged(self) -> None:
        """Ticker with positive social data preserves max_score=10 for social bucket."""
        security = Security(ticker="TEST", name="Test Co", layer=Layer.COMPUTE, benchmark="SOXX")
        trigger = TriggerResult(
            triggered=True,
            reasons=["ten_day_drawdown"],
            ten_day_drawdown=0.2,
            twenty_day_drawdown=0.25,
            relative_underperformance=0.1,
            new_low=False,
        )
        prices = make_price_bars(
            "TEST",
            [100 - i * 0.5 for i in range(25)],
            start_date=date(2026, 2, 26),
        )
        snapshot = SocialSnapshot(
            ticker="TEST",
            run_date=date(2026, 3, 26),
            recent_window_hours=72,
            baseline_days=30,
            provider_count=3,
            community_count=5,
            total_posts=100,
            informative_posts=80,
            recent_posts=30,
            baseline_posts=15,
            unique_authors=15,
            author_concentration=0.15,
            recent_stance=0.7,
            baseline_stance=0.2,
            delta=0.5,
            breadth=0.8,
            hard_negative_ratio=0.05,
            state=SocialReboundState.STRONG_REBOUND,
            score=8,
        )
        context = type(
            "Context",
            (),
            {
                "security": security,
                "benchmark_ticker": "SOXX",
                "prices": prices,
                "benchmark_prices": [],
                "official_events": [],
                "fundamentals": None,
                "macro": [],
                "source_statuses": [SourceStatus(source="stooq", success=True)],
                "social_snapshot": snapshot,
                "social_source_statuses": [SourceStatus(source="social_api", success=True)],
            },
        )()

        scorecard = build_scorecard(
            run_date=date(2026, 3, 26),
            context=context,
            trigger=trigger,
            event_tag=EventTag.COMPANY_SPECIFIC,
            peer_contexts=[],
        )

        # Verify social bucket has max=10 and positive score (not max=0)
        self.assertEqual(scorecard.social_rebound.max_score, 10)
        self.assertEqual(scorecard.social_rebound.score, 8)
        # Verify it doesn't have the no_social_sample marker
        self.assertNotIn("no_social_sample", scorecard.social_rebound.notes)
