from __future__ import annotations

from datetime import date, datetime, timezone
from unittest import TestCase

from market_sentiment.models import (
    ActionState,
    EventTag,
    FundamentalSnapshot,
    Layer,
    OfficialEvent,
    OptionContract,
    OptionSnapshot,
    Security,
    SourceStatus,
    TriggerResult,
)
from market_sentiment.pipeline import dedupe_statuses
from market_sentiment.scoring import build_scorecard, cap_state_if_data_insufficient


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
