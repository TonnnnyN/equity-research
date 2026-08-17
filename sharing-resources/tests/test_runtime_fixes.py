from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

from equity_research.config import load_config
from equity_research.models import Benchmark, FundamentalSnapshot, Layer, OfficialEvent, PriceBar, Security, SourceStatus
from equity_research.pipeline import DailyPipeline
from equity_research.sources.base import SourcePayload
from equity_research.http import HttpClient


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.toml"


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
        "EQUITY_RESEARCH_DATA_DIR": os.environ.get("EQUITY_RESEARCH_DATA_DIR"),
        "EQUITY_RESEARCH_DB_PATH": os.environ.get("EQUITY_RESEARCH_DB_PATH"),
    }
    os.environ["EQUITY_RESEARCH_DATA_DIR"] = tmp_dir
    os.environ["EQUITY_RESEARCH_DB_PATH"] = str(Path(tmp_dir) / "equity_research.db")
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
    def test_http_client_retries_on_5xx_then_succeeds(self) -> None:
        import equity_research.http
        attempt_count = [0]

        class MockResponse:
            def __init__(self, status, body):
                self.status = status
                self._body = body
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def mock_urlopen(request, timeout=None, context=None):
            attempt_count[0] += 1
            if attempt_count[0] < 3:
                # Fail first two attempts with 503
                raise HTTPError("https://example.com/x", 503, "Service Unavailable", {}, None)
            # Third attempt succeeds
            return MockResponse(200, b'{"result": "success"}')

        with patch.object(equity_research.http, "_RETRY_BACKOFF_SECONDS", (0, 0)):
            with patch.object(equity_research.http, "urlopen", side_effect=mock_urlopen):
                client = HttpClient("test-ua")
                response = client.get("https://example.com/x")

                self.assertEqual(response.status, 200)
                self.assertEqual(attempt_count[0], 3)

    def test_http_client_does_not_retry_on_4xx(self) -> None:
        import equity_research.http
        attempt_count = [0]

        def mock_urlopen(request, timeout=None, context=None):
            attempt_count[0] += 1
            # Always fail with 404
            raise HTTPError("https://example.com/x", 404, "Not Found", {}, None)

        with patch.object(equity_research.http, "_RETRY_BACKOFF_SECONDS", (0, 0)):
            with patch.object(equity_research.http, "urlopen", side_effect=mock_urlopen):
                client = HttpClient("test-ua")
                with self.assertRaises(RuntimeError) as cm:
                    client.get("https://example.com/x")

                self.assertIn("HTTP 404", str(cm.exception))
                self.assertEqual(attempt_count[0], 1)

