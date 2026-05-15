from __future__ import annotations

import os
import json
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import TestCase

from market_sentiment.config import load_config
from market_sentiment.models import (
    ActionState,
    Benchmark,
    BucketScore,
    EventTag,
    FundamentalSnapshot,
    Layer,
    MacroObservation,
    OptionSnapshot,
    OfficialEvent,
    PipelineContext,
    PriceBar,
    Security,
    SourceStatus,
    TriggerResult,
)
from market_sentiment.pipeline import DailyPipeline
from market_sentiment.scoring import build_scorecard
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "watchlist.toml"


def make_price_payload(ticker: str, closing_start: float, drop: float) -> list[PriceBar]:
    start = date(2026, 1, 1)
    bars = []
    for index in range(25):
        close = closing_start - (drop * index)
        bars.append(
            PriceBar(
                ticker=ticker,
                trading_date=start + timedelta(days=index),
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=1000 + index,
                source="stub",
            )
        )
    return bars


class PipelineTests(TestCase):
    def test_pipeline_runs_with_stubbed_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
            pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
            pipeline.config.social.enabled = False

            def fake_prices(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                drop = 1.5 if ticker in {"NVDA", "GOOG"} else 0.2
                return SourcePayload(
                    data=make_price_payload(ticker, 100.0, drop),
                    status=SourceStatus(source=f"prices:{ticker}", success=True, message="ok"),
                )

            def fake_events(ticker: str, run_date: date) -> SourcePayload[list[OfficialEvent]]:
                return SourcePayload(
                    data=[
                        OfficialEvent(
                            ticker=ticker,
                            event_time=datetime(2026, 3, 15),
                            form_type="10-Q",
                            title=f"{ticker} filed quarterly results",
                            url="https://example.com",
                            source="sec",
                        )
                    ],
                    status=SourceStatus(source=f"sec:{ticker}", success=True, message="ok"),
                )

            def fake_fred(name: str, series_id: str, run_date: date) -> SourcePayload[list[MacroObservation]]:
                return SourcePayload(
                    data=[
                        MacroObservation(
                            name=name,
                            observed_on=run_date,
                            value=4.25,
                            source="fred",
                        )
                    ],
                    status=SourceStatus(source=f"fred:{name}", success=True, message="ok"),
                )

            def fake_companyfacts(ticker: str, run_date: date) -> SourcePayload[FundamentalSnapshot]:
                return SourcePayload(
                    data=FundamentalSnapshot(
                        ticker=ticker,
                        cik="0000000001",
                        period_end=date(2025, 12, 31),
                        filed_on=date(2026, 2, 1),
                        revenue_latest=100.0,
                        revenue_previous=90.0,
                        operating_cashflow_latest=30.0,
                        operating_cashflow_previous=20.0,
                        capex_latest=10.0,
                        cash_latest=50.0,
                        debt_latest=40.0,
                        source="sec_companyfacts",
                    ),
                    status=SourceStatus(source=f"facts:{ticker}", success=True, message="ok"),
                )

            pipeline.alpha_vantage.fetch_daily_prices = fake_prices  # type: ignore[method-assign]
            pipeline.sec.fetch_recent_events = fake_events  # type: ignore[method-assign]
            pipeline.sec.fetch_company_facts = fake_companyfacts  # type: ignore[method-assign]
            pipeline.fred.fetch_series = fake_fred  # type: ignore[method-assign]
            pipeline.eia.fetch_series = lambda *args, **kwargs: SourcePayload(  # type: ignore[method-assign]
                data=[],
                status=SourceStatus(source="eia", success=True, message="ok"),
            )

            report = pipeline.run(date(2026, 3, 26))

            self.assertGreaterEqual(len(report.scorecards), 1)
            self.assertTrue(all(scorecard.triggered for scorecard in report.scorecards))
            self.assertTrue((Path(tmp) / "reports" / "2026-03-26" / "review_packets").is_dir())
            self.assertTrue(any(scorecard.fundamentals.score >= 20 for scorecard in report.scorecards))

    def test_pipeline_filters_future_price_bars_for_as_of_run_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
            pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
            pipeline.config.social.enabled = False

            def fake_prices(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                bars = make_price_payload(ticker, 100.0, 1.0)
                bars.append(
                    PriceBar(
                        ticker=ticker,
                        trading_date=date(2026, 12, 31),
                        open=10.0,
                        high=10.0,
                        low=10.0,
                        close=10.0,
                        volume=1000,
                        source="stub",
                    )
                )
                return SourcePayload(
                    data=bars,
                    status=SourceStatus(source=f"prices:{ticker}", success=True, message="ok"),
                )

            pipeline.alpha_vantage.fetch_daily_prices = fake_prices  # type: ignore[method-assign]
            pipeline.stooq.fetch_daily_prices = fake_prices  # type: ignore[method-assign]

            payload, _ = pipeline._fetch_prices_with_fallback("NVDA", date(2026, 3, 26))
            filtered = pipeline._filter_prices_as_of(payload.data, date(2026, 3, 26))

            self.assertTrue(filtered)
            self.assertLessEqual(max(bar.trading_date for bar in filtered), date(2026, 3, 26))

    def test_successful_price_fallback_does_not_force_partial_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
            pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
            pipeline.config.social.enabled = False

            def primary_failure(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                return SourcePayload(
                    data=[],
                    status=SourceStatus(source="alpha_vantage", success=False, partial=True, message="rate limited"),
                )

            def stooq_success(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                return SourcePayload(
                    data=make_price_payload(ticker, 100.0, 1.5 if ticker == "NVDA" else 0.2),
                    status=SourceStatus(source="stooq", success=True, partial=False, message="ok"),
                )

            def fake_events(ticker: str, run_date: date) -> SourcePayload[list[OfficialEvent]]:
                return SourcePayload(
                    data=[
                        OfficialEvent(
                            ticker=ticker,
                            event_time=datetime(2026, 3, 15),
                            form_type="10-Q",
                            title=f"{ticker} filed quarterly results",
                            url="https://example.com",
                            source="sec",
                        )
                    ],
                    status=SourceStatus(source=f"sec:{ticker}", success=True, message="ok"),
                )

            def fake_companyfacts(ticker: str, run_date: date) -> SourcePayload[FundamentalSnapshot]:
                return SourcePayload(
                    data=FundamentalSnapshot(
                        ticker=ticker,
                        cik="0000000001",
                        period_end=date(2025, 12, 31),
                        filed_on=date(2026, 2, 1),
                        revenue_latest=100.0,
                        revenue_previous=90.0,
                        operating_cashflow_latest=30.0,
                        operating_cashflow_previous=20.0,
                        capex_latest=10.0,
                        cash_latest=50.0,
                        debt_latest=40.0,
                        source="sec_companyfacts",
                    ),
                    status=SourceStatus(source=f"facts:{ticker}", success=True, message="ok"),
                )

            pipeline.alpha_vantage.fetch_daily_prices = primary_failure  # type: ignore[method-assign]
            pipeline.stooq.fetch_daily_prices = stooq_success  # type: ignore[method-assign]
            pipeline.sec.fetch_recent_events = fake_events  # type: ignore[method-assign]
            pipeline.sec.fetch_company_facts = fake_companyfacts  # type: ignore[method-assign]
            pipeline.fred.fetch_series = lambda *args, **kwargs: SourcePayload(  # type: ignore[method-assign]
                data=[],
                status=SourceStatus(source="fred", success=True, message="ok"),
            )
            pipeline.eia.fetch_series = lambda *args, **kwargs: SourcePayload(  # type: ignore[method-assign]
                data=[],
                status=SourceStatus(source="eia", success=True, message="ok"),
            )

            report = pipeline.run(date(2026, 3, 26))

            self.assertTrue(report.scorecards)
            self.assertTrue(all(not scorecard.partial_coverage for scorecard in report.scorecards))

    def test_pipeline_survives_optional_options_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
            pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
            pipeline.config.social.enabled = False
            pipeline.config.options.enabled = True

            def fake_prices(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                drop = 1.5 if ticker in {"NVDA", "GOOG"} else 0.2
                return SourcePayload(
                    data=make_price_payload(ticker, 100.0, drop),
                    status=SourceStatus(source=f"prices:{ticker}", success=True, message="ok"),
                )

            def fake_events(ticker: str, run_date: date) -> SourcePayload[list[OfficialEvent]]:
                return SourcePayload(
                    data=[
                        OfficialEvent(
                            ticker=ticker,
                            event_time=datetime(2026, 3, 15),
                            form_type="10-Q",
                            title=f"{ticker} filed quarterly results",
                            url="https://example.com",
                            source="sec",
                        )
                    ],
                    status=SourceStatus(source=f"sec:{ticker}", success=True, message="ok"),
                )

            def fake_fred(name: str, series_id: str, run_date: date) -> SourcePayload[list[MacroObservation]]:
                return SourcePayload(
                    data=[MacroObservation(name=name, observed_on=run_date, value=4.25, source="fred")],
                    status=SourceStatus(source=f"fred:{name}", success=True, message="ok"),
                )

            def fake_companyfacts(ticker: str, run_date: date) -> SourcePayload[FundamentalSnapshot]:
                return SourcePayload(
                    data=FundamentalSnapshot(
                        ticker=ticker,
                        cik="0000000001",
                        period_end=date(2025, 12, 31),
                        filed_on=date(2026, 2, 1),
                        revenue_latest=100.0,
                        revenue_previous=90.0,
                        operating_cashflow_latest=30.0,
                        operating_cashflow_previous=20.0,
                        capex_latest=10.0,
                        cash_latest=50.0,
                        debt_latest=40.0,
                        source="sec_companyfacts",
                    ),
                    status=SourceStatus(source=f"facts:{ticker}", success=True, message="ok"),
                )

            pipeline.alpha_vantage.fetch_daily_prices = fake_prices  # type: ignore[method-assign]
            pipeline.sec.fetch_recent_events = fake_events  # type: ignore[method-assign]
            pipeline.sec.fetch_company_facts = fake_companyfacts  # type: ignore[method-assign]
            pipeline.fred.fetch_series = fake_fred  # type: ignore[method-assign]
            pipeline.eia.fetch_series = lambda *args, **kwargs: SourcePayload(  # type: ignore[method-assign]
                data=[],
                status=SourceStatus(source="eia", success=True, message="ok"),
            )
            pipeline.options.fetch_realtime_chain = lambda *args, **kwargs: SourcePayload(  # type: ignore[method-assign]
                data=None,
                status=SourceStatus(source="alpha_vantage_options", success=False, partial=True, message="premium endpoint"),
            )

            report = pipeline.run(date(2026, 3, 26))

            self.assertGreaterEqual(len(report.scorecards), 1)
            self.assertTrue(all(not scorecard.partial_coverage for scorecard in report.scorecards))
            self.assertTrue(any(status.source == "alpha_vantage_options" for status in report.source_statuses))

    def test_pipeline_writes_option_summary_into_review_packet_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
            pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
            pipeline.config.social.enabled = False
            pipeline.config.options.enabled = True

            def fake_prices(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                drop = 1.5 if ticker in {"NVDA", "GOOG"} else 0.2
                return SourcePayload(
                    data=make_price_payload(ticker, 100.0, drop),
                    status=SourceStatus(source=f"prices:{ticker}", success=True, message="ok"),
                )

            def fake_events(ticker: str, run_date: date) -> SourcePayload[list[OfficialEvent]]:
                return SourcePayload(
                    data=[],
                    status=SourceStatus(source=f"sec:{ticker}", success=True, message="ok"),
                )

            def fake_companyfacts(ticker: str, run_date: date) -> SourcePayload[FundamentalSnapshot]:
                return SourcePayload(
                    data=FundamentalSnapshot(
                        ticker=ticker,
                        cik="0000000001",
                        period_end=date(2025, 12, 31),
                        filed_on=date(2026, 2, 1),
                        revenue_latest=100.0,
                        revenue_previous=90.0,
                        operating_cashflow_latest=30.0,
                        operating_cashflow_previous=20.0,
                        capex_latest=10.0,
                        cash_latest=50.0,
                        debt_latest=40.0,
                        source="sec_companyfacts",
                    ),
                    status=SourceStatus(source=f"facts:{ticker}", success=True, message="ok"),
                )

            pipeline.alpha_vantage.fetch_daily_prices = fake_prices  # type: ignore[method-assign]
            pipeline.stooq.fetch_daily_prices = fake_prices  # type: ignore[method-assign]
            pipeline.sec.fetch_recent_events = fake_events  # type: ignore[method-assign]
            pipeline.sec.fetch_company_facts = fake_companyfacts  # type: ignore[method-assign]
            pipeline.fred.fetch_series = lambda *args, **kwargs: SourcePayload(data=[], status=SourceStatus(source="fred", success=True, message="ok"))  # type: ignore[method-assign]
            pipeline.eia.fetch_series = lambda *args, **kwargs: SourcePayload(data=[], status=SourceStatus(source="eia", success=True, message="ok"))  # type: ignore[method-assign]
            pipeline.options.fetch_realtime_chain = lambda ticker, run_date: SourcePayload(  # type: ignore[method-assign]
                data=OptionSnapshot(
                    ticker=ticker,
                    run_date=run_date,
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
                    top_contracts=[],
                ),
                status=SourceStatus(source="alpha_vantage_options", success=True, message="ok"),
            )

            run_day = date(2026, 3, 26)
            report = pipeline.run(run_day)

            packet_dir = Path(tmp) / "reports" / run_day.isoformat() / "review_packets"
            self.assertTrue(packet_dir.is_dir())
            packet_path = packet_dir / f"{report.scorecards[0].security.ticker}.json"
            packet = json.loads(packet_path.read_text(encoding="utf-8"))

            self.assertIn("option_summary", packet)
            self.assertEqual(packet["option_summary"]["contract_count"], 2)

    def test_fetch_prices_falls_back_to_sqlite_cache_when_av_and_stooq_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
            pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
            storage = pipeline.storage
            storage.init_db()

            # Pre-seed Storage's daily_prices table with 25 bars for "AAPL" (recent dates)
            bars = []
            start_date = date.today() - timedelta(days=30)  # Start 30 days ago (within 60-day window)
            for i in range(25):
                bars.append(
                    PriceBar(
                        ticker="AAPL",
                        trading_date=start_date + timedelta(days=i),
                        open=100.0 - i * 0.5,
                        high=101.0 - i * 0.5,
                        low=99.0 - i * 0.5,
                        close=100.5 - i * 0.5,
                        volume=1000 + i * 10,
                        source="test_seed",
                    )
                )
            storage.upsert_prices(bars)

            # Mock Tiger to return empty payload (success=False)
            def tiger_failure(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                return SourcePayload(
                    data=[],
                    status=SourceStatus(source="tiger", success=False, partial=True, message="no data"),
                )

            # Mock Yahoo to return empty payload (success=False)
            def yahoo_failure(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                return SourcePayload(
                    data=[],
                    status=SourceStatus(source="yahoo_chart", success=False, partial=True, message="no data"),
                )

            # Mock Alpha Vantage to return empty payload (success=False)
            def av_failure(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                return SourcePayload(
                    data=[],
                    status=SourceStatus(source="alpha_vantage", success=False, partial=True, message="rate limited"),
                )

            # Mock Stooq to return empty payload (success=False)
            def stooq_failure(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                return SourcePayload(
                    data=[],
                    status=SourceStatus(source="stooq", success=False, partial=True, message="no data"),
                )

            pipeline.tiger.fetch_daily_prices = tiger_failure  # type: ignore[method-assign]
            pipeline.yahoo.fetch_daily_prices = yahoo_failure  # type: ignore[method-assign]
            pipeline.alpha_vantage.fetch_daily_prices = av_failure  # type: ignore[method-assign]
            pipeline.stooq.fetch_daily_prices = stooq_failure  # type: ignore[method-assign]

            # Call _fetch_prices_with_fallback (use today as the run date)
            run_date = date.today()
            payload, statuses = pipeline._fetch_prices_with_fallback("AAPL", run_date)

            # Assert: returned payload has 25 bars, status.source == "daily_prices_cache", status.partial is True
            self.assertEqual(len(payload.data), 25)
            self.assertEqual(payload.status.source, "daily_prices_cache")
            self.assertTrue(payload.status.partial)
            self.assertIn("using cached prices through", payload.status.message)

            # Assert: statuses list contains all five sources (Tiger, Yahoo, AV, Stooq, cache)
            self.assertEqual(len(statuses), 5)
            self.assertEqual(statuses[0].source, "tiger")
            self.assertEqual(statuses[1].source, "yahoo_chart")
            self.assertEqual(statuses[2].source, "alpha_vantage")
            self.assertEqual(statuses[3].source, "stooq")
            self.assertEqual(statuses[4].source, "daily_prices_cache")
            self.assertTrue(statuses[4].partial)
            self.assertIn("Tiger+Yahoo+AV+Stooq all unavailable", statuses[4].message)

    def test_pipeline_graceful_degradation_when_all_sources_fail(self) -> None:
        """
        Test that the pipeline degrades gracefully when ALL data sources fail.
        Constructs a PipelineContext with empty data on all fetched fields and all-failed SourceStatus,
        then calls build_scorecard and asserts the result is safe (data_insufficient=True, state is WATCH/REJECT).
        """
        run_date = date(2026, 3, 26)

        # Minimal security for context construction
        security = Security(
            ticker="TEST",
            name="Test Ticker",
            layer=Layer.AI_APPLICATIONS,
            benchmark="QQQ"
        )

        # All sources have failed
        source_statuses = [
            SourceStatus(source="alpha_vantage", success=False, partial=True, message="rate limited"),
            SourceStatus(source="stooq", success=False, partial=True, message="connection timeout"),
            SourceStatus(source="sec_events", success=False, partial=True, message="API error"),
            SourceStatus(source="sec_companyfacts", success=False, partial=True, message="not found"),
            SourceStatus(source="fred", success=False, partial=True, message="service unavailable"),
            SourceStatus(source="eia", success=False, partial=True, message="network error"),
            SourceStatus(source="reddit", success=False, partial=True, message="fetch failed"),
        ]

        # Construct context with empty data on all fields
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],  # No price data
            benchmark_prices=[],  # No benchmark data
            official_events=[],  # No SEC events
            fundamentals=None,  # No company facts
            macro=[],  # No macro data
            source_statuses=source_statuses,
            social_snapshot=None,  # No social data
            social_posts_sample=[],
            social_source_statuses=[],
            options_snapshot=None,
            options_source_statuses=[],
        )

        # Trigger with no actual trigger (not triggered)
        trigger = TriggerResult(triggered=False, reasons=[])

        # Peer contexts (empty for simplicity)
        peer_contexts = []

        # Build scorecard and check graceful degradation
        scorecard = build_scorecard(
            run_date=run_date,
            context=context,
            trigger=trigger,
            event_tag=EventTag.COMPANY_SPECIFIC,
            peer_contexts=peer_contexts,
        )

        # Key assertions for graceful degradation
        self.assertTrue(scorecard.data_insufficient, "Should mark data as insufficient when all sources fail")
        self.assertIn(
            scorecard.state,
            (ActionState.WATCH, ActionState.REJECT),
            "State should be WATCH or REJECT when data is insufficient"
        )
        # Should not have crashed or raised an exception
        self.assertIsNotNone(scorecard)
        self.assertEqual(scorecard.security.ticker, "TEST")
