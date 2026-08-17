"""Tests for DailyPipeline's deep-backfill / incremental price history mechanism.

Covers the three things the price-history depth change hinges on:
  - the deep-vs-shallow backfill decision (_price_fetch_plan)
  - the incremental-append path (get_price_history merges fresh + cached bars)
  - the migration case: an existing shallow/legacy cache doesn't crash and gets
    upgraded to a deep backfill on first use, rather than being assumed already deep.

All tests use fake price sources — no live network calls are made.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest import TestCase

from market_sentiment.config import PRICE_HISTORY_TARGET_DAYS, load_config
from market_sentiment.models import PriceBar, SourceStatus
from market_sentiment.pipeline import DailyPipeline
from market_sentiment.sources.base import SourcePayload

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "watchlist.toml"


def _make_pipeline(tmp: str) -> DailyPipeline:
    os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
    os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
    pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
    pipeline.storage.init_db()
    return pipeline


def _seed_bars(pipeline: DailyPipeline, ticker: str, dates: list[date]) -> None:
    bars = [
        PriceBar(
            ticker=ticker,
            trading_date=d,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1000,
            source="test_seed",
        )
        for d in dates
    ]
    pipeline.storage.upsert_prices(bars)


class PriceFetchPlanTests(TestCase):
    """Deep-vs-shallow backfill decision (DailyPipeline._price_fetch_plan)."""

    def test_needs_deep_when_no_cache_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = _make_pipeline(tmp)
            run_date = date(2026, 3, 26)

            needs_deep, _ = pipeline._price_fetch_plan("NEWTICKER", run_date)

            self.assertTrue(needs_deep)

    def test_needs_deep_for_shallow_legacy_cache(self) -> None:
        """A cache that only goes back ~30 days (e.g. from before this depth change
        shipped) must be recognized as shallow and trigger a deep backfill, not be
        mistaken for already-deep."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = _make_pipeline(tmp)
            run_date = date(2026, 3, 26)
            _seed_bars(pipeline, "LEGACY", [run_date - timedelta(days=offset) for offset in range(30)])

            needs_deep, _ = pipeline._price_fetch_plan("LEGACY", run_date)

            self.assertTrue(needs_deep)

    def test_does_not_need_deep_once_cache_already_covers_target_depth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = _make_pipeline(tmp)
            run_date = date(2026, 3, 26)
            # Earliest bar right at the target depth; latest bar is today.
            _seed_bars(
                pipeline,
                "DEEP",
                [run_date - timedelta(days=PRICE_HISTORY_TARGET_DAYS), run_date - timedelta(days=1)],
            )

            needs_deep, incremental_lookback_days = pipeline._price_fetch_plan("DEEP", run_date)

            self.assertFalse(needs_deep)
            # Gap since latest cached bar (1 day) + slack, not the full target depth.
            self.assertLess(incremental_lookback_days, PRICE_HISTORY_TARGET_DAYS)

    def test_incremental_lookback_scales_with_gap_since_latest_bar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = _make_pipeline(tmp)
            run_date = date(2026, 3, 26)
            _seed_bars(
                pipeline,
                "DEEP",
                [run_date - timedelta(days=PRICE_HISTORY_TARGET_DAYS), run_date - timedelta(days=20)],
            )

            needs_deep, incremental_lookback_days = pipeline._price_fetch_plan("DEEP", run_date)

            self.assertFalse(needs_deep)
            self.assertGreaterEqual(incremental_lookback_days, 20)
            self.assertLess(incremental_lookback_days, PRICE_HISTORY_TARGET_DAYS)


class GetPriceHistoryTests(TestCase):
    """get_price_history: fetch + cache + merge behavior."""

    def test_deep_backfill_requests_full_target_lookback(self) -> None:
        """A ticker with no cache must trigger a request for the full target depth."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = _make_pipeline(tmp)
            run_date = date(2026, 3, 26)
            captured: dict = {}

            def fake_tiger(ticker: str, _run_date: date, *, lookback_days=None) -> SourcePayload[list[PriceBar]]:
                captured["lookback_days"] = lookback_days
                bars = [
                    PriceBar(
                        ticker=ticker,
                        trading_date=run_date - timedelta(days=offset),
                        open=100.0, high=101.0, low=99.0, close=100.5, volume=1000,
                        source="tiger",
                    )
                    for offset in range(0, lookback_days or 0, 30)  # sparse but spans the window
                ]
                return SourcePayload(data=bars, status=SourceStatus(source="tiger", success=True, message="ok"))

            pipeline.tiger.fetch_daily_prices = fake_tiger  # type: ignore[method-assign]

            prices, status, _ = pipeline.get_price_history("ZM", run_date)

            self.assertEqual(captured["lookback_days"], PRICE_HISTORY_TARGET_DAYS)
            self.assertEqual(status.source, "tiger")
            self.assertTrue(prices)
            # The cache must now actually hold this deep history.
            earliest = pipeline.storage.get_earliest_cached_price_date("ZM")
            self.assertIsNotNone(earliest)
            self.assertLessEqual((run_date - earliest).days, PRICE_HISTORY_TARGET_DAYS)
            self.assertGreater((run_date - earliest).days, PRICE_HISTORY_TARGET_DAYS - 60)

    def test_incremental_path_requests_small_window_not_full_depth(self) -> None:
        """Once a ticker is already deep-backfilled, a subsequent run must only request
        the small incremental gap — NOT re-download the full multi-year history."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = _make_pipeline(tmp)
            run_date = date(2026, 3, 26)
            # Pre-seed a deep cache: earliest bar at the target depth, latest bar 3 days ago.
            _seed_bars(
                pipeline,
                "ZM",
                [
                    run_date - timedelta(days=PRICE_HISTORY_TARGET_DAYS),
                    run_date - timedelta(days=200),
                    run_date - timedelta(days=3),
                ],
            )
            captured: dict = {}

            def fake_tiger(ticker: str, _run_date: date, *, lookback_days=None) -> SourcePayload[list[PriceBar]]:
                captured["lookback_days"] = lookback_days
                return SourcePayload(
                    data=[
                        PriceBar(
                            ticker=ticker, trading_date=run_date, open=1.0, high=1.0, low=1.0,
                            close=1.0, volume=1.0, source="tiger",
                        )
                    ],
                    status=SourceStatus(source="tiger", success=True, message="ok"),
                )

            pipeline.tiger.fetch_daily_prices = fake_tiger  # type: ignore[method-assign]

            prices, _, _ = pipeline.get_price_history("ZM", run_date)

            # Requested window must be small (the gap since the latest cached bar), not
            # anywhere near the full ~5-year target depth.
            self.assertLess(captured["lookback_days"], 30)
            self.assertLess(captured["lookback_days"], PRICE_HISTORY_TARGET_DAYS)

            # The returned series must be the OLD cached bars PLUS the newly fetched one —
            # an append, not a replace.
            returned_dates = {bar.trading_date for bar in prices}
            self.assertIn(run_date - timedelta(days=PRICE_HISTORY_TARGET_DAYS), returned_dates)
            self.assertIn(run_date - timedelta(days=200), returned_dates)
            self.assertIn(run_date, returned_dates)
            self.assertEqual(len(prices), 4)

    def test_shallow_legacy_cache_upgrades_to_deep_backfill_without_crashing(self) -> None:
        """Migration case: an existing shallow database (e.g. from before this depth
        change) must not be assumed already deep, and must not crash — it should
        trigger exactly one deep backfill and end up with deep history afterward."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = _make_pipeline(tmp)
            run_date = date(2026, 3, 26)
            # Legacy shallow cache: only ~20 days deep (pre-dating this feature).
            _seed_bars(pipeline, "ACMR", [run_date - timedelta(days=offset) for offset in range(20)])
            captured: dict = {}

            def fake_tiger(ticker: str, _run_date: date, *, lookback_days=None) -> SourcePayload[list[PriceBar]]:
                captured["lookback_days"] = lookback_days
                bars = [
                    PriceBar(
                        ticker=ticker,
                        trading_date=run_date - timedelta(days=offset),
                        open=100.0, high=101.0, low=99.0, close=100.5, volume=1000,
                        source="tiger",
                    )
                    for offset in range(0, lookback_days or 0, 30)
                ]
                return SourcePayload(data=bars, status=SourceStatus(source="tiger", success=True, message="ok"))

            pipeline.tiger.fetch_daily_prices = fake_tiger  # type: ignore[method-assign]

            # Must not raise.
            prices, status, _ = pipeline.get_price_history("ACMR", run_date)

            self.assertEqual(captured["lookback_days"], PRICE_HISTORY_TARGET_DAYS)
            self.assertTrue(prices)
            earliest = pipeline.storage.get_earliest_cached_price_date("ACMR")
            self.assertGreater((run_date - earliest).days, 20)  # deeper than the legacy 20-day cache
