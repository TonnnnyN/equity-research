from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import TestCase

from market_sentiment.config import load_config
from market_sentiment.models import Benchmark, FundamentalSnapshot, Layer, OfficialEvent, PriceBar, Security, SourceStatus
from market_sentiment.pipeline import DailyPipeline
from market_sentiment.sources.base import SourcePayload


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "watchlist.toml"


def make_price_bars(ticker: str, closes: list[float], start: date = date(2026, 3, 1)) -> list[PriceBar]:
    bars: list[PriceBar] = []
    for index, close in enumerate(closes):
        bars.append(
            PriceBar(
                ticker=ticker,
                trading_date=start + timedelta(days=index),
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=1_000 + index,
                source="stub",
            )
        )
    return bars


def make_pipeline(tmp_dir: str) -> tuple[DailyPipeline, dict[str, str | None]]:
    old_env = {
        "MARKET_SENTIMENT_DATA_DIR": os.environ.get("MARKET_SENTIMENT_DATA_DIR"),
        "MARKET_SENTIMENT_DB_PATH": os.environ.get("MARKET_SENTIMENT_DB_PATH"),
    }
    os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp_dir
    os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp_dir) / "market_sentiment.db")
    config = load_config(str(CONFIG_PATH))
    config.securities = [Security(ticker="NVDA", name="NVIDIA", layer=Layer.COMPUTE, benchmark="SOXX")]
    config.benchmarks = {"SOXX": Benchmark(ticker="SOXX", name="SOXX")}
    config.fred_series = {}
    config.eia_series = {}
    return DailyPipeline(config), old_env


def restore_env(old_env: dict[str, str | None]) -> None:
    for key, value in old_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class RuntimeFixTests(TestCase):
    def test_historical_run_filters_future_data_before_scoring_and_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, old_env = make_pipeline(tmp)
            try:
                run_date = date(2026, 3, 26)

                security_prices = make_price_bars(
                    "NVDA",
                    [100 - (index * 1.2) for index in range(26)] + [130, 132, 134, 136],
                )
                benchmark_prices = make_price_bars(
                    "SOXX",
                    [100 - (index * 0.4) for index in range(26)] + [110, 111, 112, 113],
                )

                def fetch_prices(ticker: str, _run_date: date) -> SourcePayload[list[PriceBar]]:
                    payload = security_prices if ticker == "NVDA" else benchmark_prices
                    return SourcePayload(
                        data=payload,
                        status=SourceStatus(source=f"alpha_vantage:{ticker}", success=True, message="ok"),
                    )

                def unexpected_fallback(*_args, **_kwargs) -> SourcePayload[list[PriceBar]]:
                    raise AssertionError("fallback should not be used in this scenario")

                def fetch_events(ticker: str, _run_date: date) -> SourcePayload[list[OfficialEvent]]:
                    return SourcePayload(
                        data=[
                            OfficialEvent(
                                ticker=ticker,
                                event_time=datetime(2026, 3, 30),
                                form_type="8-K",
                                title="future disclosure",
                                url="https://example.com/future",
                                source="sec",
                            )
                        ],
                        status=SourceStatus(source=f"sec:{ticker}", success=True, message="ok"),
                    )

                def fetch_companyfacts(ticker: str, _run_date: date) -> SourcePayload[FundamentalSnapshot]:
                    return SourcePayload(
                        data=FundamentalSnapshot(
                            ticker=ticker,
                            cik="0000000001",
                            period_end=date(2026, 3, 31),
                            filed_on=date(2026, 4, 1),
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

                pipeline.alpha_vantage.fetch_daily_prices = fetch_prices  # type: ignore[method-assign]
                pipeline.stooq.fetch_daily_prices = unexpected_fallback  # type: ignore[method-assign]
                pipeline.sec.fetch_recent_events = fetch_events  # type: ignore[method-assign]
                pipeline.sec.fetch_company_facts = fetch_companyfacts  # type: ignore[method-assign]

                report = pipeline.run(run_date)

                self.assertEqual(report.triggered_count, 1)
                self.assertTrue(report.scorecards)
                self.assertTrue(report.scorecards[0].triggered)

                db_path = Path(tmp) / "market_sentiment.db"
                with sqlite3.connect(db_path) as conn:
                    self.assertLessEqual(
                        conn.execute("SELECT COALESCE(MAX(trading_date), '') FROM daily_prices").fetchone()[0],
                        run_date.isoformat(),
                    )
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM official_events").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM fundamental_snapshots").fetchone()[0], 0)
            finally:
                restore_env(old_env)

    def test_successful_benchmark_fallback_does_not_mark_scorecards_partial_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, old_env = make_pipeline(tmp)
            try:
                run_date = date(2026, 3, 26)

                security_prices = make_price_bars("NVDA", [100 - (index * 1.2) for index in range(26)])
                benchmark_prices = make_price_bars("SOXX", [100 - (index * 0.4) for index in range(26)])

                def fetch_prices(ticker: str, _run_date: date) -> SourcePayload[list[PriceBar]]:
                    if ticker == "SOXX":
                        return SourcePayload(
                            data=[],
                            status=SourceStatus(source="alpha_vantage:SOXX", success=False, partial=True, message="primary unavailable"),
                        )
                    return SourcePayload(
                        data=security_prices,
                        status=SourceStatus(source="alpha_vantage:NVDA", success=True, message="ok"),
                    )

                def fetch_fallback(ticker: str, _run_date: date) -> SourcePayload[list[PriceBar]]:
                    payload = benchmark_prices if ticker == "SOXX" else security_prices
                    return SourcePayload(
                        data=payload,
                        status=SourceStatus(source=f"stooq:{ticker}", success=True, message="ok"),
                    )

                pipeline.alpha_vantage.fetch_daily_prices = fetch_prices  # type: ignore[method-assign]
                pipeline.stooq.fetch_daily_prices = fetch_fallback  # type: ignore[method-assign]
                pipeline.sec.fetch_recent_events = lambda *args, **kwargs: SourcePayload(  # type: ignore[method-assign]
                    data=[],
                    status=SourceStatus(source="sec:events", success=True, message="ok"),
                )
                pipeline.sec.fetch_company_facts = lambda *args, **kwargs: SourcePayload(  # type: ignore[method-assign]
                    data=None,
                    status=SourceStatus(source="sec:facts", success=True, message="ok"),
                )

                report = pipeline.run(run_date)

                self.assertTrue(report.scorecards)
                self.assertTrue(report.scorecards[0].triggered)
                self.assertFalse(report.scorecards[0].partial_coverage)
            finally:
                restore_env(old_env)

    def test_preflight_flags_missing_x_credentials_and_options_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, old_env = make_pipeline(tmp)
            original_alpha_key = os.environ.pop("ALPHAVANTAGE_API_KEY", None)
            try:
                pipeline.config.social.enabled = True
                pipeline.config.social.x.enabled = True
                pipeline.config.social.x.provider = "twscrape,twikit"
                pipeline.config.social.x.db_path = str(Path(tmp) / "twscrape_accounts.db")
                pipeline.config.social.x.accounts_file = None
                pipeline.config.social.x.cookies_path = None
                pipeline.config.social.x.username = None
                pipeline.config.social.x.password = None
                pipeline.config.options.enabled = True

                summary = pipeline.preflight()
                rendered = "\n".join(summary.to_lines()).lower()

                self.assertFalse(summary.ready)
                self.assertIn("x runtime", rendered)
                self.assertIn("no usable x credential", rendered)
                self.assertIn("options", rendered)
                self.assertIn("alphavantage_api_key", rendered)
            finally:
                if original_alpha_key is not None:
                    os.environ["ALPHAVANTAGE_API_KEY"] = original_alpha_key
                restore_env(old_env)

    def test_preflight_accepts_twscrape_bootstrap_file_as_ready_x_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, old_env = make_pipeline(tmp)
            try:
                accounts_file = Path(tmp) / "x_accounts.txt"
                accounts_file.write_text(
                    "user:pass:mail@example.com:mailpass:_:cookies.json\n",
                    encoding="utf-8",
                )
                pipeline.config.social.enabled = True
                pipeline.config.social.x.enabled = True
                pipeline.config.social.x.provider = "twscrape,twikit"
                pipeline.config.social.x.db_path = str(Path(tmp) / "twscrape_accounts.db")
                pipeline.config.social.x.accounts_file = str(accounts_file)
                pipeline.config.social.x.cookies_path = None
                pipeline.config.social.x.username = None
                pipeline.config.social.x.password = None
                pipeline.config.options.enabled = False

                summary = pipeline.preflight()
                rendered = "\n".join(summary.to_lines()).lower()

                self.assertTrue(summary.ready)
                self.assertIn("accounts bootstrap", rendered)
            finally:
                restore_env(old_env)
