from __future__ import annotations

import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import TestCase

from market_sentiment.models import PriceBar, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.yahoo_finance import YahooFinanceClient
from market_sentiment.storage import Storage


class FakeResponse:
    def __init__(self, url: str, payload):
        self.url = url
        self.safe_url = url
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, payload: dict):
        self._payload = payload
        self.last_url = ""
        self.last_params = None

    def get(self, url: str, params=None, headers=None):
        self.last_url = url
        self.last_params = params
        return FakeResponse(url, self._payload)


class YahooFinanceClientTests(TestCase):
    """Test cases for the YahooFinanceClient."""

    def test_successful_fetch_parses_chart_payload(self) -> None:
        """Test that successful API call parses chart payload to PriceBar objects."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            # Create fake payload with 3 days of price data
            payload = {
                "chart": {
                    "result": [
                        {
                            "timestamp": [
                                1715596800,  # 2024-05-13 00:00:00 UTC
                                1715683200,  # 2024-05-14 00:00:00 UTC
                                1715769600,  # 2024-05-15 00:00:00 UTC
                            ],
                            "indicators": {
                                "quote": [
                                    {
                                        "open": [100.0, 101.5, 102.3],
                                        "high": [101.0, 102.5, 103.2],
                                        "low": [99.5, 100.8, 101.5],
                                        "close": [100.5, 102.0, 102.8],
                                        "volume": [1000000.0, 1100000.0, 950000.0],
                                    }
                                ]
                            },
                        }
                    ]
                }
            }

            http_client = FakeHttpClient(payload)
            client = YahooFinanceClient(http_client, storage)

            result = client.fetch_daily_prices("MSFT", date(2026, 5, 15))

            # Verify success
            self.assertTrue(result.status.success)
            self.assertFalse(result.status.partial)
            self.assertEqual(result.status.source, "yahoo_chart")
            self.assertEqual(result.status.message, "ok")

            # Verify price data
            self.assertEqual(len(result.data), 3)

            # All should have source == "yahoo_chart"
            for price_bar in result.data:
                self.assertEqual(price_bar.source, "yahoo_chart")
                self.assertEqual(price_bar.ticker, "MSFT")

            # Verify prices are in ascending date order
            dates = [pb.trading_date for pb in result.data]
            self.assertEqual(dates, sorted(dates))

            # Verify first price bar values
            first = result.data[0]
            self.assertEqual(first.open, 100.0)
            self.assertEqual(first.high, 101.0)
            self.assertEqual(first.low, 99.5)
            self.assertEqual(first.close, 100.5)
            self.assertEqual(first.volume, 1000000.0)

    def test_yahoo_chart_error_returns_partial(self) -> None:
        """Test that chart error in response returns partial failure."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            payload = {
                "chart": {
                    "error": {
                        "description": "Not Found",
                    }
                }
            }

            http_client = FakeHttpClient(payload)
            client = YahooFinanceClient(http_client, storage)

            result = client.fetch_daily_prices("INVALID", date(2026, 5, 15))

            # Verify partial failure
            self.assertFalse(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertEqual(result.status.source, "yahoo_chart")
            self.assertIn("Not Found", result.status.message)
            self.assertEqual(result.data, [])

    def test_empty_timestamp_returns_partial(self) -> None:
        """Test that empty timestamp/quote data returns partial failure."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            payload = {
                "chart": {
                    "result": [
                        {
                            "timestamp": [],
                            "indicators": {
                                "quote": [
                                    {
                                        "open": [],
                                        "high": [],
                                        "low": [],
                                        "close": [],
                                        "volume": [],
                                    }
                                ]
                            },
                        }
                    ]
                }
            }

            http_client = FakeHttpClient(payload)
            client = YahooFinanceClient(http_client, storage)

            result = client.fetch_daily_prices("MSFT", date(2026, 5, 15))

            # Verify partial failure
            self.assertFalse(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertEqual(result.status.source, "yahoo_chart")
            self.assertIn("empty", result.status.message.lower())
            self.assertEqual(result.data, [])

    def test_hk_ticker_is_passed_through_unchanged(self) -> None:
        """Test that HK ticker is passed through without symbol conversion."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            payload = {
                "chart": {
                    "result": [
                        {
                            "timestamp": [1715596800],
                            "indicators": {
                                "quote": [
                                    {
                                        "open": [500.0],
                                        "high": [510.0],
                                        "low": [495.0],
                                        "close": [505.0],
                                        "volume": [2000000.0],
                                    }
                                ]
                            },
                        }
                    ]
                }
            }

            http_client = FakeHttpClient(payload)
            client = YahooFinanceClient(http_client, storage)

            result = client.fetch_daily_prices("9660.HK", date(2026, 5, 15))

            # Verify success
            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 1)

            # Original ticker should be preserved
            self.assertEqual(result.data[0].ticker, "9660.HK")
            self.assertEqual(result.data[0].source, "yahoo_chart")

            # Verify URL encoding was used (9660.HK should be URL-encoded)
            # The FakeHttpClient doesn't validate URL structure, but we can check
            # that the client was called (last_url is set)
            self.assertIsNotNone(http_client.last_url)
            # Verify the params were sent correctly
            self.assertEqual(http_client.last_params["interval"], "1d")
            self.assertEqual(http_client.last_params["range"], "6mo")

    def test_closed_prices_none_are_skipped(self) -> None:
        """Test that rows with None close prices are skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            payload = {
                "chart": {
                    "result": [
                        {
                            "timestamp": [1715596800, 1715683200, 1715769600],
                            "indicators": {
                                "quote": [
                                    {
                                        "open": [100.0, 101.5, 102.3],
                                        "high": [101.0, 102.5, 103.2],
                                        "low": [99.5, 100.8, 101.5],
                                        "close": [100.5, None, 102.8],  # Middle row has None close
                                        "volume": [1000000.0, 1100000.0, 950000.0],
                                    }
                                ]
                            },
                        }
                    ]
                }
            }

            http_client = FakeHttpClient(payload)
            client = YahooFinanceClient(http_client, storage)

            result = client.fetch_daily_prices("MSFT", date(2026, 5, 15))

            # Should only have 2 bars (first and third), not the middle one with None close
            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 2)
            self.assertEqual(result.data[0].close, 100.5)
            self.assertEqual(result.data[1].close, 102.8)

    def test_lookback_days_none_preserves_default_six_month_range(self) -> None:
        """Omitting lookback_days must keep requesting Yahoo's `range=6mo` shortcut —
        the historical default — so any caller that doesn't need depth control sees
        unchanged behavior."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            payload = {"chart": {"result": [{"timestamp": [1715596800], "indicators": {"quote": [{"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]}]}}]}}
            http_client = FakeHttpClient(payload)
            client = YahooFinanceClient(http_client, storage)

            client.fetch_daily_prices("MSFT", date(2026, 5, 15))

            self.assertEqual(http_client.last_params["range"], "6mo")
            self.assertNotIn("period1", http_client.last_params)
            self.assertNotIn("period2", http_client.last_params)

    def test_lookback_days_requests_explicit_period_window_instead_of_range(self) -> None:
        """A given lookback_days must switch to explicit period1/period2 timestamps
        (instead of Yahoo's coarse `range` presets) so both a small incremental gap
        and a multi-year deep backfill can be requested precisely."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            payload = {"chart": {"result": [{"timestamp": [1715596800], "indicators": {"quote": [{"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]}]}}]}}
            http_client = FakeHttpClient(payload)
            client = YahooFinanceClient(http_client, storage)

            run_date = date(2026, 5, 15)
            result = client.fetch_daily_prices("MSFT", run_date, lookback_days=1825)

            self.assertTrue(result.status.success)
            self.assertNotIn("range", http_client.last_params)
            self.assertIn("period1", http_client.last_params)
            self.assertIn("period2", http_client.last_params)

            period1 = int(http_client.last_params["period1"])
            period2 = int(http_client.last_params["period2"])
            span_days = (period2 - period1) / 86400
            self.assertAlmostEqual(span_days, 1825, delta=1)

    def test_short_incremental_lookback_days_produces_narrow_window(self) -> None:
        """A small lookback_days (the incremental top-up case) must produce a narrow
        period1/period2 window, not the full deep-backfill span."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            payload = {"chart": {"result": [{"timestamp": [1715596800], "indicators": {"quote": [{"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]}]}}]}}
            http_client = FakeHttpClient(payload)
            client = YahooFinanceClient(http_client, storage)

            client.fetch_daily_prices("MSFT", date(2026, 5, 15), lookback_days=7)

            period1 = int(http_client.last_params["period1"])
            period2 = int(http_client.last_params["period2"])
            span_days = (period2 - period1) / 86400
            self.assertAlmostEqual(span_days, 7, delta=1)
