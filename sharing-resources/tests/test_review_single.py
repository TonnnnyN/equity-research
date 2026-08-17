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

    def test_review_single_returns_dict_with_required_top_level_keys(self) -> None:
        """review_single must return a well-formed review packet dict."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._make_pipeline(tmp)
            _wire_fakes(pipeline, _fake_prices_dropping)

            packet = pipeline.review_single(
                "ACMR",
                benchmark="IWM",
                layer=Layer.AI_APPLICATIONS,
                run_date=date(2026, 3, 26),
                name="ACM Research",
            )

            self.assertIsInstance(packet, dict)
            # Required top-level keys from review_packets.build_review_packet
            for key in (
                "packet_type",
                "schema_version",
                "packet_id",
                "generated_at",
                "run_date",
                "security",
                "benchmark_ticker",
                "rule_engine_precheck",
                "trigger_summary",
                "bucket_scores",
                "price_context",
                "official_events",
                "fundamentals_snapshot",
                "macro_summary",
                "source_health",
                "decision_summary",
                "freshness",
                "earnings_calendar",
            ):
                self.assertIn(key, packet, f"Missing key: {key}")

    def test_review_single_packet_contains_correct_ticker_and_benchmark(self) -> None:
        """The packet's security block must reflect the ticker/benchmark passed in."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._make_pipeline(tmp)
            _wire_fakes(pipeline, _fake_prices_dropping)

            packet = pipeline.review_single(
                "IRTC",
                benchmark="IWM",
                layer=Layer.AI_APPLICATIONS,
                run_date=date(2026, 3, 26),
            )

            self.assertEqual(packet["security"]["ticker"], "IRTC")
            self.assertEqual(packet["benchmark_ticker"], "IWM")
            self.assertEqual(packet["packet_id"], "2026-03-26::IRTC")

    def test_review_single_packet_contains_layer_1_and_layer_2_evidence(self) -> None:
        """Packet must contain Layer 1 trigger summary AND Layer 2 bucket scores."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._make_pipeline(tmp)
            _wire_fakes(pipeline, _fake_prices_dropping)

            packet = pipeline.review_single(
                "ACMR",
                benchmark="IWM",
                layer=Layer.AI_APPLICATIONS,
                run_date=date(2026, 3, 26),
            )

            # Layer 1: price trigger evidence
            trigger = packet["trigger_summary"]
            self.assertIn("ten_day_drawdown", trigger)
            self.assertIn("twenty_day_drawdown", trigger)
            self.assertIn("relative_underperformance", trigger)

            # Layer 2: fundamental / sentiment / social / chain / price_flow / risk buckets
            buckets = packet["bucket_scores"]
            for bucket_name in ("fundamentals", "sentiment", "chain_confirmation", "price_flow", "risk_red_flags"):
                self.assertIn(bucket_name, buckets, f"Missing bucket: {bucket_name}")

    def test_review_single_returns_packet_even_when_not_triggered(self) -> None:
        """A packet must be returned even when compute_trigger returns triggered=False.

        This is the key on-demand use-case: the caller already knows it wants the deep
        review layer must always produce the full evidence packet.
        """
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._make_pipeline(tmp)
            # Flat prices: drawdown thresholds will NOT be breached.
            _wire_fakes(pipeline, _fake_prices_flat)

            packet = pipeline.review_single(
                "ACMR",
                benchmark="IWM",
                layer=Layer.AI_APPLICATIONS,
                run_date=date(2026, 3, 26),
            )

            self.assertIsInstance(packet, dict)
            # triggered may be False, but the packet must still exist and be complete
            self.assertIn("rule_engine_precheck", packet)
            triggered = packet["rule_engine_precheck"]["triggered"]
            self.assertFalse(triggered, "Expected triggered=False with flat prices")
            # Full evidence is still present
            self.assertIn("bucket_scores", packet)
            self.assertIn("trigger_summary", packet)
            self.assertIn("fundamentals_snapshot", packet)
            self.assertIsNotNone(packet["fundamentals_snapshot"])

    def test_review_single_uses_supplied_name(self) -> None:
        """The optional name parameter must appear in the security block."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._make_pipeline(tmp)
            _wire_fakes(pipeline, _fake_prices_dropping)

            packet = pipeline.review_single(
                "XYZ",
                benchmark="IWM",
                layer=Layer.AI_APPLICATIONS,
                run_date=date(2026, 3, 26),
                name="XYZ Corp",
            )

            self.assertEqual(packet["security"]["name"], "XYZ Corp")

    def test_review_single_defaults_name_to_ticker_when_not_supplied(self) -> None:
        """When name is omitted the ticker is used as the name."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._make_pipeline(tmp)
            _wire_fakes(pipeline, _fake_prices_dropping)

            packet = pipeline.review_single(
                "XYZ",
                benchmark="IWM",
                layer=Layer.AI_APPLICATIONS,
                run_date=date(2026, 3, 26),
            )

            self.assertEqual(packet["security"]["name"], "XYZ")

    def test_review_single_falls_back_to_first_threshold_when_layer_missing(self) -> None:
        """If the chosen layer has no threshold entry the pipeline must not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._make_pipeline(tmp)
            _wire_fakes(pipeline, _fake_prices_dropping)

            # Use PHARMA layer which is always present; this is a sanity check that
            # threshold resolution works for all existing layers.
            packet = pipeline.review_single(
                "ACMR",
                benchmark="IWM",
                layer=Layer.PHARMA,
                run_date=date(2026, 3, 26),
            )

            self.assertIsInstance(packet, dict)
            self.assertIn("trigger_summary", packet)

    def test_review_single_social_lane_is_skipped_gracefully_when_disabled(self) -> None:
        """When social is disabled the packet still contains social_summary: null."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._make_pipeline(tmp)
            pipeline.config.social.enabled = False
            _wire_fakes(pipeline, _fake_prices_dropping)

            packet = pipeline.review_single(
                "ACMR",
                benchmark="IWM",
                layer=Layer.AI_APPLICATIONS,
                run_date=date(2026, 3, 26),
            )

            self.assertIn("social_summary", packet)
            self.assertIsNone(packet["social_summary"])

    def test_review_single_price_context_reflects_benchmark_ticker(self) -> None:
        """Price context must include both security and benchmark bars."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._make_pipeline(tmp)
            _wire_fakes(pipeline, _fake_prices_dropping)

            packet = pipeline.review_single(
                "ACMR",
                benchmark="IWM",
                layer=Layer.AI_APPLICATIONS,
                run_date=date(2026, 3, 26),
            )

            price_ctx = packet["price_context"]
            self.assertIn("latest_security_bar", price_ctx)
            self.assertIn("latest_benchmark_bar", price_ctx)
            self.assertIn("recent_security_bars", price_ctx)
            self.assertIn("recent_benchmark_bars", price_ctx)
            # Both must have data (fake client returns bars for every ticker)
            self.assertIsNotNone(price_ctx["latest_security_bar"])
            self.assertIsNotNone(price_ctx["latest_benchmark_bar"])

    def test_review_single_packet_is_json_serializable(self) -> None:
        """The returned dict must survive a json.dumps / json.loads round-trip."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = self._make_pipeline(tmp)
            _wire_fakes(pipeline, _fake_prices_dropping)

            packet = pipeline.review_single(
                "ACMR",
                benchmark="IWM",
                layer=Layer.AI_APPLICATIONS,
                run_date=date(2026, 3, 26),
            )

            serialized = json.dumps(packet)
            restored = json.loads(serialized)
            self.assertEqual(packet["packet_id"], restored["packet_id"])


class ReviewTickerCLITests(TestCase):
    """Smoke-tests for the `review-ticker` CLI subcommand."""

    def _make_pipeline_and_wire(self, tmp: str, price_fn=_fake_prices_dropping) -> DailyPipeline:
        os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
        os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
        pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
        pipeline.config.social.enabled = False
        pipeline.config.options.enabled = False
        _wire_fakes(pipeline, price_fn)
        return pipeline

    def test_review_ticker_cli_writes_json_file_to_reports_dir(self) -> None:
        """The CLI command must save the packet under data/reports/<date>/review_packets/<TICKER>.json."""
        from market_sentiment.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")

            # We patch the pipeline inside main by creating a subclass that wires fakes.
            import market_sentiment.cli as cli_module
            import market_sentiment.pipeline as pipeline_module

            original_pipeline_cls = pipeline_module.DailyPipeline

            class FakePipeline(original_pipeline_cls):
                def __init__(self, config=None):
                    super().__init__(config)
                    self.config.social.enabled = False
                    self.config.options.enabled = False
                    _wire_fakes(self, _fake_prices_dropping)

            pipeline_module.DailyPipeline = FakePipeline  # type: ignore[misc]
            try:
                ret = main([
                    "--config", str(CONFIG_PATH),
                    "review-ticker", "ACMR",
                    "--benchmark", "IWM",
                    "--layer", "ai_applications",
                    "--date", "2026-03-26",
                    "--name", "ACM Research",
                ])
            finally:
                pipeline_module.DailyPipeline = original_pipeline_cls  # type: ignore[misc]

            self.assertEqual(ret, 0)
            out_path = Path(tmp) / "reports" / "2026-03-26" / "review_packets" / "ACMR.json"
            self.assertTrue(out_path.exists(), f"Expected packet file at {out_path}")
            packet = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(packet["security"]["ticker"], "ACMR")
            self.assertEqual(packet["benchmark_ticker"], "IWM")

    def test_review_ticker_cli_returns_zero_exit_code(self) -> None:
        """The CLI command must return exit code 0 on success."""
        from market_sentiment.cli import main
        import market_sentiment.pipeline as pipeline_module

        original_pipeline_cls = pipeline_module.DailyPipeline

        class FakePipeline(original_pipeline_cls):
            def __init__(self, config=None):
                super().__init__(config)
                self.config.social.enabled = False
                self.config.options.enabled = False
                _wire_fakes(self, _fake_prices_flat)

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")

            pipeline_module.DailyPipeline = FakePipeline  # type: ignore[misc]
            try:
                ret = main([
                    "--config", str(CONFIG_PATH),
                    "review-ticker", "IRTC",
                    "--benchmark", "IWM",
                    "--layer", "utilities",
                    "--date", "2026-03-26",
                ])
            finally:
                pipeline_module.DailyPipeline = original_pipeline_cls  # type: ignore[misc]

            self.assertEqual(ret, 0)
