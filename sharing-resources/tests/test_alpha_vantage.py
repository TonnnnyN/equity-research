from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from market_sentiment.sources.alpha_vantage import AlphaVantageClient
from market_sentiment.storage import Storage


class FakeResponse:
    def __init__(self, url: str, payload: dict):
        self.url = url
        self.safe_url = url
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeHttpClient:
    def __init__(self, payload: dict):
        self._payload = payload
        self.last_url = ""
        self.last_params: dict | None = None

    def get(self, url: str, params=None, headers=None):
        self.last_url = url
        self.last_params = params
        return FakeResponse(url, self._payload)


_SAMPLE_PAYLOAD = {
    "Time Series (Daily Adjusted)": {
        "2026-05-13": {
            "1. open": "100.0",
            "2. high": "101.0",
            "3. low": "99.5",
            "5. adjusted close": "100.5",
            "6. volume": "1000000",
        },
        "2026-05-14": {
            "1. open": "101.5",
            "2. high": "102.5",
            "3. low": "100.8",
            "5. adjusted close": "102.0",
            "6. volume": "1100000",
        },
    }
}


class AlphaVantageClientTests(TestCase):
    """outputsize toggle: compact (incremental default) vs full (deep backfill)."""

    def test_deep_false_default_requests_compact_outputsize(self) -> None:
        """The historical default: omitting `deep` must keep requesting
        outputsize=compact (last ~100 points) — cheap and appropriate for routine
        incremental top-ups given Alpha Vantage's tight free-tier rate limit."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()
            http_client = FakeHttpClient(_SAMPLE_PAYLOAD)

            with patch.dict("os.environ", {"ALPHAVANTAGE_API_KEY": "test-key"}, clear=False):
                client = AlphaVantageClient(http_client, storage)
                result = client.fetch_daily_prices("MSFT", date(2026, 5, 15))

            self.assertTrue(result.status.success)
            self.assertEqual(http_client.last_params["outputsize"], "compact")
            self.assertEqual(len(result.data), 2)

    def test_deep_true_requests_full_outputsize(self) -> None:
        """deep=True must request outputsize=full (Alpha Vantage's 20+-year series)
        for a one-time deep backfill."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()
            http_client = FakeHttpClient(_SAMPLE_PAYLOAD)

            with patch.dict("os.environ", {"ALPHAVANTAGE_API_KEY": "test-key"}, clear=False):
                client = AlphaVantageClient(http_client, storage)
                result = client.fetch_daily_prices("MSFT", date(2026, 5, 15), deep=True)

            self.assertTrue(result.status.success)
            self.assertEqual(http_client.last_params["outputsize"], "full")
