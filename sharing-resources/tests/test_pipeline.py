from __future__ import annotations

import os
import json
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import TestCase

from market_sentiment.config import load_config
from market_sentiment.decision_tracker import DecisionAlert
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

    def test_track_decisions_ingests_valid_decision_file(self) -> None:
        """Test that a valid .decision.json file is ingested into the active_decisions table."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
            pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
            pipeline.storage.init_db()

            # Create a valid decision file with a condition that won't fire (price would need to DROP 5%)
            decisions_subdir = Path(tmp) / "decisions" / "2026-03-15"
            decisions_subdir.mkdir(parents=True, exist_ok=True)
            decision_payload = {
                "ticker": "AAPL",
                "decision_date": "2026-03-15",
                "state": "WATCH",
                "reference_close": 150.0,
                "invalidate_conditions": [
                    {
                        "metric": "pct_from_reference",
                        "comparator": "<",  # Trigger if price drops (negative return)
                        "threshold": -0.05,  # drops 5% or more
                        "window": 1,
                        "note": "down 5% from entry",
                    }
                ],
                "rerate_conditions": [],
            }
            decision_file = decisions_subdir / "AAPL_2026-03-15.decision.json"
            decision_file.write_text(json.dumps(decision_payload), encoding="utf-8")

            # Run tracking pass
            result = pipeline._track_decisions(date(2026, 3, 20))

            # Assert: decision was ingested and remains active (condition did not fire)
            all_aapl = pipeline.storage.read_all_decisions_for_ticker("AAPL")
            self.assertEqual(len(all_aapl), 1)
            self.assertEqual(all_aapl[0]["ticker"], "AAPL")
            self.assertEqual(all_aapl[0]["decision_date"], "2026-03-15")
            self.assertEqual(all_aapl[0]["status"], "active")
            self.assertEqual(len(result.ingest_warnings), 0)
            self.assertEqual(len(result.alerts), 0)  # No alerts because condition didn't fire

    def test_track_decisions_skips_malformed_files_with_warnings(self) -> None:
        """Test that malformed decision files produce warnings and are not ingested."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
            pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
            pipeline.storage.init_db()

            # Create a malformed decision file (missing required fields)
            decisions_subdir = Path(tmp) / "decisions" / "2026-03-15"
            decisions_subdir.mkdir(parents=True, exist_ok=True)
            bad_payload = {
                "ticker": "AAPL",
                # Missing decision_date, state, reference_close, conditions
            }
            decision_file = decisions_subdir / "AAPL_bad.decision.json"
            decision_file.write_text(json.dumps(bad_payload), encoding="utf-8")

            # Run tracking pass
            result = pipeline._track_decisions(date(2026, 3, 20))

            # Assert: no decision was ingested, but warning was generated
            active = pipeline.storage.read_active_decisions()
            self.assertEqual(len(active), 0)
            self.assertGreater(len(result.ingest_warnings), 0)

    def test_track_decisions_does_not_resurrect_invalidated_decisions(self) -> None:
        """Test that an already-invalidated decision in the table is not re-upserted when file is re-scanned."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
            pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
            pipeline.storage.init_db()

            # Pre-insert an invalidated decision
            pipeline.storage.upsert_active_decision(
                ticker="GOOG",
                decision_date="2026-03-10",
                state="WATCH",
                reference_close=140.0,
                invalidate_conditions=[],
                rerate_conditions=[],
                status="invalidated",
                status_reason="price went up",
            )

            # Create a decision file with the same (ticker, decision_date)
            decisions_subdir = Path(tmp) / "decisions" / "2026-03-10"
            decisions_subdir.mkdir(parents=True, exist_ok=True)
            decision_payload = {
                "ticker": "GOOG",
                "decision_date": "2026-03-10",
                "state": "WATCH",
                "reference_close": 140.0,
                "invalidate_conditions": [],
                "rerate_conditions": [],
            }
            decision_file = decisions_subdir / "GOOG_2026-03-10.decision.json"
            decision_file.write_text(json.dumps(decision_payload), encoding="utf-8")

            # Run tracking pass
            result = pipeline._track_decisions(date(2026, 3, 20))

            # Assert: status is still "invalidated" (not "active")
            all_for_goog = pipeline.storage.read_all_decisions_for_ticker("GOOG")
            self.assertEqual(len(all_for_goog), 1)
            self.assertEqual(all_for_goog[0]["status"], "invalidated")
            self.assertEqual(all_for_goog[0]["status_reason"], "price went up")

    def test_track_decisions_swallows_errors_and_returns_empty_result(self) -> None:
        """Test that internal errors are swallowed and the method returns safely."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
            pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
            pipeline.storage.init_db()

            # Break the storage by pointing to a non-existent db path (will cause errors on read)
            pipeline.storage.db_path = Path(tmp) / "nonexistent" / "broken.db"

            # Run tracking pass; should not raise
            result = pipeline._track_decisions(date(2026, 3, 20))

            # Assert: got a result with a warning, not an exception
            self.assertIsNotNone(result)
            self.assertGreater(len(result.ingest_warnings), 0)
