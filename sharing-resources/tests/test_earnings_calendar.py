from __future__ import annotations

import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import TestCase

from equity_research.models import EarningsCalendar, SourceStatus
from equity_research.sources.base import SourcePayload
from equity_research.sources.earnings_calendar import EarningsCalendarClient
from equity_research.storage import Storage


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


class EarningsCalendarClientTests(TestCase):
    """Test cases for the EarningsCalendarClient."""

    def test_successful_fetch_with_future_earnings_date(self) -> None:
        """Test that successful API call returns EarningsCalendar with future date."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            # Create fake payload with a future earnings date
            # 1783123200 = 2026-07-04 (about 7 weeks from 2026-05-15)
            payload = {
                "quoteSummary": {
                    "result": [
                        {
                            "calendarEvents": {
                                "earnings": {
                                    "earningsDate": [
                                        {"raw": 1783123200, "fmt": "2026-07-04"}
                                    ],
                                    "isEarningsDateEstimate": True,
                                }
                            }
                        }
                    ],
                    "error": None,
                }
            }

            http_client = FakeHttpClient(payload)
            client = EarningsCalendarClient(http_client, storage)

            result = client.fetch_next_earnings("MSFT", date(2026, 5, 15))

            # Verify success
            self.assertTrue(result.status.success)
            self.assertFalse(result.status.partial)
            self.assertEqual(result.status.source, "yahoo_earnings_calendar")
            self.assertEqual(result.status.message, "ok")

            # Verify earnings calendar data
            self.assertIsNotNone(result.data)
            self.assertEqual(result.data.ticker, "MSFT")
            self.assertEqual(result.data.next_earnings_date, date(2026, 7, 4))
            self.assertTrue(result.data.is_estimate)

    def test_past_earnings_date_returns_none(self) -> None:
        """Test that past earnings date is filtered out."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            # Create fake payload with a past earnings date
            # 1715558400 = 2024-05-13 (in the past relative to run_date 2026-05-15)
            payload = {
                "quoteSummary": {
                    "result": [
                        {
                            "calendarEvents": {
                                "earnings": {
                                    "earningsDate": [
                                        {"raw": 1715558400, "fmt": "2024-05-13"}
                                    ],
                                    "isEarningsDateEstimate": False,
                                }
                            }
                        }
                    ],
                    "error": None,
                }
            }

            http_client = FakeHttpClient(payload)
            client = EarningsCalendarClient(http_client, storage)

            result = client.fetch_next_earnings("MSFT", date(2026, 5, 15))

            # Verify failure with past date
            self.assertFalse(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertEqual(result.status.source, "yahoo_earnings_calendar")
            self.assertIn("in the past", result.status.message)
            self.assertIsNone(result.data)

    def test_empty_earnings_date_array_returns_none(self) -> None:
        """Test that empty earningsDate array returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            payload = {
                "quoteSummary": {
                    "result": [
                        {
                            "calendarEvents": {
                                "earnings": {
                                    "earningsDate": [],
                                    "isEarningsDateEstimate": False,
                                }
                            }
                        }
                    ],
                    "error": None,
                }
            }

            http_client = FakeHttpClient(payload)
            client = EarningsCalendarClient(http_client, storage)

            result = client.fetch_next_earnings("MSFT", date(2026, 5, 15))

            # Verify partial success with None data
            self.assertFalse(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertEqual(result.status.source, "yahoo_earnings_calendar")
            self.assertIn("no earnings date", result.status.message)
            self.assertIsNone(result.data)

    def test_http_500_error_returns_failed(self) -> None:
        """Test that HTTP 500 error returns failed status."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            class FailingHttpClient:
                def get(self, url: str, params=None, headers=None):
                    raise RuntimeError("HTTP 500 Internal Server Error")

            http_client = FailingHttpClient()
            client = EarningsCalendarClient(http_client, storage)

            result = client.fetch_next_earnings("MSFT", date(2026, 5, 15))

            # Verify failure
            self.assertFalse(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertEqual(result.status.source, "yahoo_earnings_calendar")
            self.assertIn("Failed to fetch", result.status.message)
            self.assertIsNone(result.data)

    def test_non_json_response_returns_failed(self) -> None:
        """Test that non-JSON response returns failed status."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            class BadJsonHttpClient:
                def __init__(self):
                    self.url = "http://example.com"
                    self.safe_url = "http://example.com"

                def get(self, url: str, params=None, headers=None):
                    response = FakeResponse(url, None)
                    # Mock json() to raise an error
                    response.json = lambda: (_ for _ in ()).throw(ValueError("Invalid JSON"))
                    return response

            http_client = BadJsonHttpClient()
            client = EarningsCalendarClient(http_client, storage)

            result = client.fetch_next_earnings("MSFT", date(2026, 5, 15))

            # Verify failure
            self.assertFalse(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertEqual(result.status.source, "yahoo_earnings_calendar")
            self.assertIn("Failed to parse", result.status.message)
            self.assertIsNone(result.data)

    def test_hk_ticker_not_supported(self) -> None:
        """Test that .HK tickers are not supported."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            http_client = FakeHttpClient({})
            client = EarningsCalendarClient(http_client, storage)

            result = client.fetch_next_earnings("9660.HK", date(2026, 5, 15))

            # Verify failure
            self.assertFalse(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertEqual(result.status.source, "yahoo_earnings_calendar")
            self.assertIn("Non-US ticker", result.status.message)
            self.assertIsNone(result.data)

    def test_api_error_in_response_returns_failed(self) -> None:
        """Test that API error in quoteSummary returns failed status."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            payload = {
                "quoteSummary": {
                    "result": [],
                    "error": {"description": "No matching quote found"},
                }
            }

            http_client = FakeHttpClient(payload)
            client = EarningsCalendarClient(http_client, storage)

            result = client.fetch_next_earnings("INVALID_TICKER", date(2026, 5, 15))

            # Verify failure
            self.assertFalse(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertEqual(result.status.source, "yahoo_earnings_calendar")
            self.assertIn("No matching quote", result.status.message)
            self.assertIsNone(result.data)

    def test_is_estimate_flag_preserved(self) -> None:
        """Test that isEarningsDateEstimate flag is preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            # Test with is_estimate = False
            # 1783123200 = 2026-07-04
            payload = {
                "quoteSummary": {
                    "result": [
                        {
                            "calendarEvents": {
                                "earnings": {
                                    "earningsDate": [
                                        {"raw": 1783123200, "fmt": "2026-07-04"}
                                    ],
                                    "isEarningsDateEstimate": False,
                                }
                            }
                        }
                    ],
                    "error": None,
                }
            }

            http_client = FakeHttpClient(payload)
            client = EarningsCalendarClient(http_client, storage)

            result = client.fetch_next_earnings("MSFT", date(2026, 5, 15))

            # Verify flag
            self.assertTrue(result.status.success)
            self.assertIsNotNone(result.data)
            self.assertFalse(result.data.is_estimate)

    def test_fetched_at_timestamp_set(self) -> None:
        """Test that fetched_at timestamp is set correctly."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            # 1783123200 = 2026-07-04
            payload = {
                "quoteSummary": {
                    "result": [
                        {
                            "calendarEvents": {
                                "earnings": {
                                    "earningsDate": [
                                        {"raw": 1783123200, "fmt": "2026-07-04"}
                                    ],
                                    "isEarningsDateEstimate": True,
                                }
                            }
                        }
                    ],
                    "error": None,
                }
            }

            http_client = FakeHttpClient(payload)
            client = EarningsCalendarClient(http_client, storage)

            before = datetime.now(timezone.utc)
            result = client.fetch_next_earnings("MSFT", date(2026, 5, 15))
            after = datetime.now(timezone.utc)

            # Verify fetched_at is set and within bounds
            self.assertIsNotNone(result.data)
            self.assertGreaterEqual(result.data.fetched_at, before)
            self.assertLessEqual(result.data.fetched_at, after)
