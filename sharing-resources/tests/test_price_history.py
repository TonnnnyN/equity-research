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

from equity_research.config import PRICE_HISTORY_TARGET_DAYS, load_config
from equity_research.models import PriceBar, SourceStatus
from equity_research.pipeline import DailyPipeline
from equity_research.sources.base import SourcePayload

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.toml"


def _make_pipeline(tmp: str) -> DailyPipeline:
    os.environ["EQUITY_RESEARCH_DATA_DIR"] = tmp
    os.environ["EQUITY_RESEARCH_DB_PATH"] = str(Path(tmp) / "equity_research.db")
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

