"""Tests for DailyPipeline.review_single and the `review-ticker` CLI command.

All tests use fake data sources — no real network calls are made.
Test style follows test_pipeline.py conventions.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import TestCase

from market_sentiment.config import load_config
from market_sentiment.models import (
    FundamentalSnapshot,
    Layer,
    MacroObservation,
    OfficialEvent,
    PriceBar,
    SourceStatus,
)
from market_sentiment.pipeline import DailyPipeline
from market_sentiment.sources.base import SourcePayload


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "watchlist.toml"


def _make_bars(ticker: str, start_close: float, drop: float, count: int = 25) -> list[PriceBar]:
    """Build a sequence of PriceBars with a constant daily drop."""
    start = date(2026, 1, 1)
    return [
        PriceBar(
            ticker=ticker,
            trading_date=start + timedelta(days=i),
            open=start_close - drop * i,
            high=start_close - drop * i + 1,
            low=start_close - drop * i - 1,
            close=start_close - drop * i,
            volume=1000 + i,
            source="stub",
        )
        for i in range(count)
    ]


def _fake_prices_dropping(ticker: str, run_date: date, **_kwargs) -> SourcePayload[list[PriceBar]]:
    """Return price bars with a significant downtrend (triggers a drawdown trigger)."""
    drop = 2.0  # ~15% drawdown over 25 bars from 100
    return SourcePayload(
        data=_make_bars(ticker, 100.0, drop),
        status=SourceStatus(source=f"prices:{ticker}", success=True, message="ok"),
    )


def _fake_prices_flat(ticker: str, run_date: date, **_kwargs) -> SourcePayload[list[PriceBar]]:
    """Return price bars with almost no price movement (trigger will NOT fire)."""
    return SourcePayload(
        data=_make_bars(ticker, 100.0, 0.05),
        status=SourceStatus(source=f"prices:{ticker}", success=True, message="ok"),
    )


def _fake_events(ticker: str, run_date: date) -> SourcePayload[list[OfficialEvent]]:
    return SourcePayload(
        data=[
            OfficialEvent(
                ticker=ticker,
                event_time=datetime(2026, 3, 15),
                form_type="10-Q",
                title=f"{ticker} quarterly filing",
                url="https://example.com",
                source="sec",
            )
        ],
        status=SourceStatus(source=f"sec:{ticker}", success=True, message="ok"),
    )


def _fake_companyfacts(ticker: str, run_date: date) -> SourcePayload[FundamentalSnapshot]:
    return SourcePayload(
        data=FundamentalSnapshot(
            ticker=ticker,
            cik="0000099999",
            period_end=date(2025, 12, 31),
            filed_on=date(2026, 2, 1),
            revenue_latest=200.0,
            revenue_previous=180.0,
            operating_cashflow_latest=40.0,
            operating_cashflow_previous=35.0,
            capex_latest=10.0,
            cash_latest=80.0,
            debt_latest=50.0,
            source="sec_companyfacts",
        ),
        status=SourceStatus(source=f"facts:{ticker}", success=True, message="ok"),
    )


def _fake_fred(name: str, series_id: str, run_date: date) -> SourcePayload[list[MacroObservation]]:
    return SourcePayload(
        data=[MacroObservation(name=name, observed_on=run_date, value=4.25, source="fred")],
        status=SourceStatus(source=f"fred:{name}", success=True, message="ok"),
    )


def _noop_eia(*args, **kwargs) -> SourcePayload[list[MacroObservation]]:
    return SourcePayload(
        data=[],
        status=SourceStatus(source="eia", success=True, message="ok"),
    )


def _fake_valuation_fundamentals(ticker: str, run_date: date) -> SourcePayload[None]:
    return SourcePayload(
        data=None,
        status=SourceStatus(source="sec_valuation_fundamentals", success=False, partial=True, message="stubbed"),
    )


def _wire_fakes(pipeline: DailyPipeline, price_fn) -> None:
    """Attach fake fetch methods to every price client on the pipeline."""
    pipeline.tiger.fetch_daily_prices = price_fn  # type: ignore[method-assign]
    pipeline.yahoo.fetch_daily_prices = price_fn  # type: ignore[method-assign]
    pipeline.alpha_vantage.fetch_daily_prices = price_fn  # type: ignore[method-assign]
    pipeline.stooq.fetch_daily_prices = price_fn  # type: ignore[method-assign]
    pipeline.sec.fetch_recent_events = _fake_events  # type: ignore[method-assign]
    pipeline.sec.fetch_company_facts = _fake_companyfacts  # type: ignore[method-assign]
    pipeline.sec.fetch_valuation_fundamentals = _fake_valuation_fundamentals  # type: ignore[method-assign]
    pipeline.fred.fetch_series = _fake_fred  # type: ignore[method-assign]
    pipeline.eia.fetch_series = _noop_eia  # type: ignore[method-assign]


class ReviewSingleTests(TestCase):
    # ------------------------------------------------------------------ helpers

    def _make_pipeline(self, tmp: str) -> DailyPipeline:
        os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
        os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
        pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
        pipeline.config.social.enabled = False
        pipeline.config.options.enabled = False
        return pipeline

    # ------------------------------------------------------------------ tests

    def _make_pipeline_and_wire(self, tmp: str, price_fn=_fake_prices_dropping) -> DailyPipeline:
        os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
        os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
        pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
        pipeline.config.social.enabled = False
        pipeline.config.options.enabled = False
        _wire_fakes(pipeline, price_fn)
        return pipeline

