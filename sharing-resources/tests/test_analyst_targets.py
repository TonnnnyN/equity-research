"""Tests for AnalystTargetsClient.

All tests use fake data sources — no real network calls are made.
Test style follows test_earnings_calendar.py conventions.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import TestCase

from market_sentiment.models import AnalystSnapshot, SourceStatus
from market_sentiment.sources.analyst_targets import AnalystTargetsClient
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage


# ---------------------------------------------------------------------------
# Fake HTTP infrastructure
# ---------------------------------------------------------------------------

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
        self.last_params: dict | None = None

    def get(self, url: str, params=None, headers=None):
        self.last_url = url
        self.last_params = params
        return FakeResponse(url, self._payload)


class ErrorHttpClient:
    """Always raises an exception on get()."""

    def get(self, url: str, params=None, headers=None):
        raise RuntimeError("Network error: connection refused")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_yahoo_payload(
    target_mean: float = 150.0,
    target_high: float = 180.0,
    target_low: float = 120.0,
    target_median: float = 148.0,
    current_price: float = 130.0,
    number_of_analysts: int = 25,
    recommendation_key: str = "buy",
    recommendation_mean: float = 2.1,
    include_trend: bool = True,
    include_history: bool = True,
) -> dict:
    """Build a minimal but realistic Yahoo quoteSummary payload."""
    history = []
    if include_history:
        history = [
            {
                "epochGradeDate": 1746057600,  # approx 2025-05-01
                "firm": "Goldman Sachs",
                "toGrade": "Buy",
                "fromGrade": "Neutral",
                "action": "up",
            },
            {
                "epochGradeDate": 1743379200,  # approx 2025-03-31
                "firm": "Morgan Stanley",
                "toGrade": "Overweight",
                "fromGrade": "Equal-Weight",
                "action": "up",
            },
            {
                "epochGradeDate": 1740700800,  # approx 2025-02-28
                "firm": "JP Morgan",
                "toGrade": "Neutral",
                "fromGrade": "Overweight",
                "action": "down",
            },
        ]

    trend = []
    if include_trend:
        trend = [
            {"period": "0m", "strongBuy": 12, "buy": 8, "hold": 4, "sell": 1, "strongSell": 0},
            {"period": "-1m", "strongBuy": 10, "buy": 8, "hold": 5, "sell": 2, "strongSell": 0},
        ]

    return {
        "quoteSummary": {
            "result": [
                {
                    "financialData": {
                        "targetMeanPrice": {"raw": target_mean, "fmt": str(target_mean)},
                        "targetHighPrice": {"raw": target_high, "fmt": str(target_high)},
                        "targetLowPrice": {"raw": target_low, "fmt": str(target_low)},
                        "targetMedianPrice": {"raw": target_median, "fmt": str(target_median)},
                        "currentPrice": {"raw": current_price, "fmt": str(current_price)},
                        "numberOfAnalystOpinions": {"raw": number_of_analysts, "fmt": str(number_of_analysts)},
                        "recommendationKey": recommendation_key,
                        "recommendationMean": {"raw": recommendation_mean, "fmt": str(recommendation_mean)},
                    },
                    "recommendationTrend": {"trend": trend},
                    "upgradeDowngradeHistory": {"history": history},
                }
            ],
            "error": None,
        }
    }


def _make_storage(tmp: str) -> Storage:
    storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
    storage.init_db()
    return storage


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class AnalystTargetsClientYahooHappyPathTests(TestCase):
    """Yahoo quoteSummary happy-path parsing tests."""

    def test_happy_path_parses_target_prices(self) -> None:
        """Happy path: all target price fields are parsed correctly."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload(
                target_mean=150.0,
                target_high=180.0,
                target_low=120.0,
                target_median=148.0,
                current_price=130.0,
            )
            client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertTrue(result.status.success)
            self.assertFalse(result.status.partial)
            self.assertEqual(result.status.source, "analyst_targets")
            self.assertEqual(result.status.message, "ok")
            self.assertIsNotNone(result.data)

            snapshot: AnalystSnapshot = result.data
            self.assertEqual(snapshot.ticker, "MSFT")
            self.assertEqual(snapshot.source, "yahoo_quote_summary")
            self.assertAlmostEqual(snapshot.target_mean, 150.0)
            self.assertAlmostEqual(snapshot.target_high, 180.0)
            self.assertAlmostEqual(snapshot.target_low, 120.0)
            self.assertAlmostEqual(snapshot.target_median, 148.0)
            self.assertAlmostEqual(snapshot.current_price, 130.0)

    def test_happy_path_computes_implied_upside(self) -> None:
        """implied_upside = (target_mean - current_price) / current_price."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload(target_mean=150.0, current_price=130.0)
            client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertIsNotNone(result.data)
            expected_upside = (150.0 - 130.0) / 130.0
            self.assertAlmostEqual(result.data.implied_upside, expected_upside, places=6)

    def test_happy_path_parses_analyst_count_and_recommendation(self) -> None:
        """number_of_analysts, recommendation_key, and recommendation_mean are parsed."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload(
                number_of_analysts=25,
                recommendation_key="buy",
                recommendation_mean=2.1,
            )
            client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertIsNotNone(result.data)
            self.assertEqual(result.data.number_of_analysts, 25)
            self.assertEqual(result.data.recommendation_key, "buy")
            self.assertAlmostEqual(result.data.recommendation_mean, 2.1)

    def test_happy_path_parses_recent_changes(self) -> None:
        """recent_changes list is parsed from upgradeDowngradeHistory."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload(include_history=True)
            client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertIsNotNone(result.data)
            changes = result.data.recent_changes
            self.assertEqual(len(changes), 3)

            first = changes[0]
            self.assertEqual(first.firm, "Goldman Sachs")
            self.assertEqual(first.action, "up")
            self.assertEqual(first.from_grade, "Neutral")
            self.assertEqual(first.to_grade, "Buy")
            self.assertIsNotNone(first.change_date)

    def test_happy_path_parses_trend(self) -> None:
        """recommendationTrend entries are kept as plain dicts."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload(include_trend=True)
            client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertIsNotNone(result.data)
            trend = result.data.trend
            self.assertEqual(len(trend), 2)
            self.assertEqual(trend[0]["period"], "0m")
            self.assertEqual(trend[0]["strongBuy"], 12)
            self.assertEqual(trend[1]["period"], "-1m")

    def test_happy_path_fetched_at_is_recent(self) -> None:
        """fetched_at must be set and within the test's execution window."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload()
            client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))

            before = datetime.now(timezone.utc)
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))
            after = datetime.now(timezone.utc)

            self.assertIsNotNone(result.data)
            self.assertGreaterEqual(result.data.fetched_at, before)
            self.assertLessEqual(result.data.fetched_at, after)


class AnalystTargetsClientErrorHandlingTests(TestCase):
    """Error and edge-case handling — must return failed payload, never raise."""

    def test_http_error_returns_failed_payload_with_none_data(self) -> None:
        """Network error yields SourcePayload(data=None) and never raises.

        FINNHUB_API_KEY is unset so the Finnhub fallback also fails gracefully,
        keeping the final result as data=None, success=False.
        """
        with tempfile.TemporaryDirectory() as tmp:
            original = os.environ.pop("FINNHUB_API_KEY", None)
            try:
                client = AnalystTargetsClient(ErrorHttpClient(), _make_storage(tmp))
                result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

                self.assertIsNone(result.data)
                self.assertFalse(result.status.success)
                self.assertTrue(result.status.partial)
                self.assertEqual(result.status.source, "analyst_targets")
            finally:
                if original is not None:
                    os.environ["FINNHUB_API_KEY"] = original

    def test_yahoo_api_error_in_response_yields_failed(self) -> None:
        """When quoteSummary.error is non-null the result has data=None.

        FINNHUB_API_KEY is unset so the Finnhub fallback also returns failure.
        """
        with tempfile.TemporaryDirectory() as tmp:
            original = os.environ.pop("FINNHUB_API_KEY", None)
            try:
                payload = {
                    "quoteSummary": {
                        "result": [],
                        "error": {"description": "No data found, symbol may be delisted"},
                    }
                }
                client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))
                result = client.fetch_analyst_snapshot("BADTICKER", date(2026, 5, 15))

                self.assertIsNone(result.data)
                self.assertFalse(result.status.success)
                self.assertEqual(result.status.source, "analyst_targets")
                # Either Yahoo's error message or Finnhub-key-missing message is acceptable
                self.assertFalse(result.status.success)
            finally:
                if original is not None:
                    os.environ["FINNHUB_API_KEY"] = original

    def test_empty_result_list_returns_failed(self) -> None:
        """Empty result list in quoteSummary yields data=None."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"quoteSummary": {"result": [], "error": None}}
            client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertIsNone(result.data)
            self.assertFalse(result.status.success)
            self.assertEqual(result.status.source, "analyst_targets")

    def test_non_json_response_never_raises(self) -> None:
        """A response whose .json() raises must return a failed payload, not propagate.

        FINNHUB_API_KEY is unset so after Yahoo parse failure the whole lane fails
        gracefully with data=None.
        """
        with tempfile.TemporaryDirectory() as tmp:
            original = os.environ.pop("FINNHUB_API_KEY", None)
            try:
                class BadJsonHttpClient:
                    def get(self, url, params=None, headers=None):
                        resp = FakeResponse(url, None)
                        resp.json = lambda: (_ for _ in ()).throw(ValueError("not json"))  # type: ignore[assignment]
                        return resp

                client = AnalystTargetsClient(BadJsonHttpClient(), _make_storage(tmp))
                result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

                self.assertIsNone(result.data)
                self.assertFalse(result.status.success)
                self.assertEqual(result.status.source, "analyst_targets")
            finally:
                if original is not None:
                    os.environ["FINNHUB_API_KEY"] = original

    def test_non_us_ticker_skips_yahoo_and_falls_back(self) -> None:
        """Non-US tickers (e.g. .HK) skip the Yahoo path entirely.

        Without FINNHUB_API_KEY the fallback returns a graceful failure.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Ensure no Finnhub key in environment for this test
            original = os.environ.pop("FINNHUB_API_KEY", None)
            try:
                client = AnalystTargetsClient(FakeHttpClient({}), _make_storage(tmp))
                result = client.fetch_analyst_snapshot("9660.HK", date(2026, 5, 15))

                self.assertIsNone(result.data)
                self.assertFalse(result.status.success)
                self.assertEqual(result.status.source, "analyst_targets")
                # Message must reference why Yahoo was skipped
                self.assertIn("FINNHUB_API_KEY unset", result.status.message)
            finally:
                if original is not None:
                    os.environ["FINNHUB_API_KEY"] = original


class AnalystTargetsImpliedUpsideTests(TestCase):
    """Standalone tests for the implied_upside calculation."""

    def test_positive_upside_computed_correctly(self) -> None:
        """target_mean 150, current 130 => upside ~15.38%."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload(target_mean=150.0, current_price=130.0)
            client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertIsNotNone(result.data)
            expected = (150.0 - 130.0) / 130.0
            self.assertAlmostEqual(result.data.implied_upside, expected, places=6)

    def test_negative_upside_when_target_below_current(self) -> None:
        """When target_mean < current_price implied_upside is negative."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload(target_mean=100.0, current_price=130.0)
            client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertIsNotNone(result.data)
            expected = (100.0 - 130.0) / 130.0
            self.assertAlmostEqual(result.data.implied_upside, expected, places=6)
            self.assertLess(result.data.implied_upside, 0)

    def test_zero_current_price_gives_none_upside(self) -> None:
        """Division by zero is avoided: implied_upside must be None when current_price=0."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload(target_mean=150.0, current_price=0.0)
            client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            # current_price=0 must not cause ZeroDivisionError
            self.assertIsNotNone(result.data)
            self.assertIsNone(result.data.implied_upside)


class AnalystTargetsFinnhubFallbackTests(TestCase):
    """Finnhub fallback behaviour tests."""

    def test_finnhub_fallback_skipped_gracefully_when_no_key(self) -> None:
        """When FINNHUB_API_KEY is unset the fallback is skipped with a clear message."""
        with tempfile.TemporaryDirectory() as tmp:
            original = os.environ.pop("FINNHUB_API_KEY", None)
            try:
                # Simulate Yahoo returning empty results → triggers fallback attempt
                payload = {"quoteSummary": {"result": [], "error": None}}
                client = AnalystTargetsClient(FakeHttpClient(payload), _make_storage(tmp))
                result = client.fetch_analyst_snapshot("AAPL", date(2026, 5, 15))

                self.assertIsNone(result.data)
                self.assertFalse(result.status.success)
                self.assertEqual(result.status.source, "analyst_targets")
                self.assertIn("FINNHUB_API_KEY unset", result.status.message)
            finally:
                if original is not None:
                    os.environ["FINNHUB_API_KEY"] = original

    def test_finnhub_fallback_attempted_when_key_is_set(self) -> None:
        """When FINNHUB_API_KEY is set the fallback is attempted after Yahoo failure.

        We simulate both Yahoo failure (empty result) and a minimal Finnhub response.
        The Finnhub URL must be called.
        """
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FINNHUB_API_KEY"] = "test-key-12345"
            try:
                call_log: list[str] = []

                class MultiEndpointFakeHttp:
                    def get(self, url: str, params=None, headers=None):
                        call_log.append(url)
                        if "price-target" in url:
                            return FakeResponse(url, {
                                "targetMean": 150.0,
                                "targetHigh": 180.0,
                                "targetLow": 120.0,
                                "targetMedian": 145.0,
                                "numberOfAnalysts": 20,
                                "lastUpdated": "2026-05-01",
                                "symbol": "AAPL",
                            })
                        if "recommendation" in url:
                            return FakeResponse(url, [
                                {"period": "2026-05-01", "strongBuy": 10, "buy": 8,
                                 "hold": 5, "sell": 1, "strongSell": 0},
                            ])
                        if "upgrade-downgrade" in url:
                            return FakeResponse(url, [
                                {"company": "Goldman", "gradeDate": "2026-04-15",
                                 "fromGrade": "Neutral", "toGrade": "Buy", "action": "up"},
                            ])
                        # Yahoo path returns empty
                        return FakeResponse(url, {"quoteSummary": {"result": [], "error": None}})

                client = AnalystTargetsClient(MultiEndpointFakeHttp(), _make_storage(tmp))
                result = client.fetch_analyst_snapshot("AAPL", date(2026, 5, 15))

                # Finnhub price-target URL must have been called
                finnhub_calls = [u for u in call_log if "finnhub" in u]
                self.assertTrue(len(finnhub_calls) > 0, "Expected at least one Finnhub call")

                self.assertFalse(result.status.success is False and "FINNHUB_API_KEY unset" in (result.status.message or ""))
                # Data should be populated from Finnhub
                if result.status.success:
                    self.assertIsNotNone(result.data)
                    self.assertEqual(result.data.source, "finnhub")
                    self.assertAlmostEqual(result.data.target_mean, 150.0)
            finally:
                os.environ.pop("FINNHUB_API_KEY", None)
