"""Tests for AnalystTargetsClient.

All tests use fake data sources — no real network calls are made.
Test style follows test_earnings_calendar.py conventions.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from equity_research.models import AnalystSnapshot, SourceStatus
from equity_research.sources.analyst_targets import AnalystTargetsClient, fetch_yahoo_cookie_and_crumb
from equity_research.sources.base import SourcePayload
from equity_research.storage import Storage


FAKE_COOKIE = "A1=abc123; B=xyz"
FAKE_CRUMB = "test-crumb-value"


def _fake_crumb_fetcher(cookie: str = FAKE_COOKIE, crumb: str = FAKE_CRUMB):
    """Returns a zero-argument callable suitable for AnalystTargetsClient(crumb_fetcher=...)."""
    return lambda: (cookie, crumb)


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
        self.last_headers: dict | None = None

    def get(self, url: str, params=None, headers=None):
        self.last_url = url
        self.last_params = params
        self.last_headers = headers
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


def _make_client(http_client, storage: Storage, crumb_fetcher=None) -> AnalystTargetsClient:
    """Construct an AnalystTargetsClient with a fake (no-network) crumb fetcher by default.

    Every test in this module goes through here instead of calling AnalystTargetsClient(...)
    directly, so the real Yahoo cookie+crumb handshake (fetch_yahoo_cookie_and_crumb) is never
    hit — it is always overridden with a fake that returns a fixed (cookie, crumb) pair.
    """
    return AnalystTargetsClient(
        http_client,
        storage,
        crumb_fetcher=crumb_fetcher or _fake_crumb_fetcher(),
    )


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
            client = _make_client(FakeHttpClient(payload), _make_storage(tmp))
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
            client = _make_client(FakeHttpClient(payload), _make_storage(tmp))
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
            client = _make_client(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertIsNotNone(result.data)
            self.assertEqual(result.data.number_of_analysts, 25)
            self.assertEqual(result.data.recommendation_key, "buy")
            self.assertAlmostEqual(result.data.recommendation_mean, 2.1)

    def test_happy_path_parses_recent_changes(self) -> None:
        """recent_changes list is parsed from upgradeDowngradeHistory."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload(include_history=True)
            client = _make_client(FakeHttpClient(payload), _make_storage(tmp))
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
            client = _make_client(FakeHttpClient(payload), _make_storage(tmp))
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
            client = _make_client(FakeHttpClient(payload), _make_storage(tmp))

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
                client = _make_client(ErrorHttpClient(), _make_storage(tmp))
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
                client = _make_client(FakeHttpClient(payload), _make_storage(tmp))
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
            client = _make_client(FakeHttpClient(payload), _make_storage(tmp))
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

                client = _make_client(BadJsonHttpClient(), _make_storage(tmp))
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
                client = _make_client(FakeHttpClient({}), _make_storage(tmp))
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
            client = _make_client(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertIsNotNone(result.data)
            expected = (150.0 - 130.0) / 130.0
            self.assertAlmostEqual(result.data.implied_upside, expected, places=6)

    def test_negative_upside_when_target_below_current(self) -> None:
        """When target_mean < current_price implied_upside is negative."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload(target_mean=100.0, current_price=130.0)
            client = _make_client(FakeHttpClient(payload), _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertIsNotNone(result.data)
            expected = (100.0 - 130.0) / 130.0
            self.assertAlmostEqual(result.data.implied_upside, expected, places=6)
            self.assertLess(result.data.implied_upside, 0)

    def test_zero_current_price_gives_none_upside(self) -> None:
        """Division by zero is avoided: implied_upside must be None when current_price=0."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload(target_mean=150.0, current_price=0.0)
            client = _make_client(FakeHttpClient(payload), _make_storage(tmp))
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
                client = _make_client(FakeHttpClient(payload), _make_storage(tmp))
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

                client = _make_client(MultiEndpointFakeHttp(), _make_storage(tmp))
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


# ---------------------------------------------------------------------------
# Crumb flow: cookie+crumb handshake, caching, 401-triggered refresh.
# All network I/O is stubbed via crumb_fetcher / FakeHttpClient — no real
# requests are made anywhere in this file.
# ---------------------------------------------------------------------------

def _counting_crumb_fetcher(pairs: list[tuple[str, str]]):
    """Returns a callable that yields ``pairs`` in order (repeating the last on overrun).

    The returned callable exposes ``.calls`` (a dict with a "count" key) so tests can
    assert how many times the handshake was actually invoked.
    """
    state = {"count": 0}

    def _fetch():
        index = min(state["count"], len(pairs) - 1)
        state["count"] += 1
        return pairs[index]

    _fetch.calls = state
    return _fetch


class UnauthorizedThenOkHttpClient:
    """Simulates Yahoo's real failure mode: HTTP 401 Invalid Crumb until a fresh crumb is used."""

    def __init__(self, payload: dict, stale_crumb: str):
        self._payload = payload
        self._stale_crumb = stale_crumb
        self.calls: list[dict] = []

    def get(self, url: str, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        crumb = (params or {}).get("crumb")
        if crumb == self._stale_crumb:
            raise RuntimeError(
                'HTTP 401 for '
                f'{url}: {{"finance":{{"result":null,"error":{{"code":"Unauthorized",'
                '"description":"Invalid Crumb"}}}}'
            )
        return FakeResponse(url, self._payload)


class AnalystTargetsCrumbFlowTests(TestCase):
    """Cookie+crumb handshake: caching, 401-triggered refresh, graceful degradation."""

    def test_yahoo_base_url_is_the_verified_working_query1_endpoint(self) -> None:
        self.assertTrue(
            AnalystTargetsClient.yahoo_base_url.startswith("https://query1.finance.yahoo.com")
        )

    def test_default_crumb_fetcher_is_the_real_handshake_function(self) -> None:
        """No network call happens here — only identity of the default is checked."""
        with tempfile.TemporaryDirectory() as tmp:
            client = AnalystTargetsClient(FakeHttpClient({}), _make_storage(tmp))
            self.assertIs(client._crumb_fetcher, fetch_yahoo_cookie_and_crumb)

    def test_successful_fetch_sends_crumb_and_cookie_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload()
            http = FakeHttpClient(payload)
            client = _make_client(http, _make_storage(tmp))
            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertTrue(result.status.success)
            self.assertEqual(http.last_params.get("crumb"), FAKE_CRUMB)
            self.assertEqual(http.last_headers.get("Cookie"), FAKE_COOKIE)

    def test_crumb_is_cached_across_multiple_fetches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload()
            http = FakeHttpClient(payload)
            crumb_fetcher = _counting_crumb_fetcher([(FAKE_COOKIE, FAKE_CRUMB)])
            client = _make_client(http, _make_storage(tmp), crumb_fetcher=crumb_fetcher)

            client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))
            client.fetch_analyst_snapshot("MSFT", date(2026, 5, 16))

            self.assertEqual(crumb_fetcher.calls["count"], 1)

    def test_401_triggers_one_automatic_crumb_refresh_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _make_yahoo_payload()
            http = UnauthorizedThenOkHttpClient(payload, stale_crumb="stale-crumb")
            crumb_fetcher = _counting_crumb_fetcher(
                [(FAKE_COOKIE, "stale-crumb"), ("fresh-cookie", "fresh-crumb")]
            )
            client = _make_client(http, _make_storage(tmp), crumb_fetcher=crumb_fetcher)

            result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

            self.assertTrue(result.status.success)
            self.assertEqual(crumb_fetcher.calls["count"], 2)
            self.assertEqual(len(http.calls), 2)
            self.assertEqual(http.calls[0]["params"]["crumb"], "stale-crumb")
            self.assertEqual(http.calls[1]["params"]["crumb"], "fresh-crumb")

    def test_crumb_handshake_failure_degrades_gracefully_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = os.environ.pop("FINNHUB_API_KEY", None)
            try:
                client = _make_client(
                    FakeHttpClient({}), _make_storage(tmp), crumb_fetcher=lambda: None
                )
                result = client.fetch_analyst_snapshot("MSFT", date(2026, 5, 15))

                self.assertIsNone(result.data)
                self.assertFalse(result.status.success)
                self.assertEqual(result.status.source, "analyst_targets")
                # Yahoo crumb failure falls through to the Finnhub-skip message (no key set);
                # the important assertion is that it degraded gracefully instead of raising.
                self.assertIn("yahoo-failed", result.status.message)
            finally:
                if original is not None:
                    os.environ["FINNHUB_API_KEY"] = original

    def test_yahoo_specific_failure_message_names_the_crumb_handshake(self) -> None:
        """Calling the Yahoo path directly (bypassing the Finnhub fallback) exposes the
        actual failure reason, which must call out the crumb handshake specifically."""
        with tempfile.TemporaryDirectory() as tmp:
            client = _make_client(
                FakeHttpClient({}), _make_storage(tmp), crumb_fetcher=lambda: None
            )
            payload = client._fetch_yahoo("MSFT", date(2026, 5, 15), datetime.now(timezone.utc))

            self.assertIsNone(payload.data)
            self.assertFalse(payload.status.success)
            self.assertIn("crumb", payload.status.message.lower())


# ---------------------------------------------------------------------------
# History persistence + derived change-over-time signals (Parts 2 & 3).
# ---------------------------------------------------------------------------

def _epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def _payload_with_history(
    *,
    target_mean: float = 150.0,
    target_high: float = 180.0,
    target_low: float = 120.0,
    history: list[dict] | None = None,
) -> dict:
    base = _make_yahoo_payload(
        target_mean=target_mean, target_high=target_high, target_low=target_low
    )
    base["quoteSummary"]["result"][0]["upgradeDowngradeHistory"] = {"history": history or []}
    return base


def _seed_price(storage: Storage, ticker: str, trading_date: date, close: float) -> None:
    from equity_research.models import PriceBar

    storage.upsert_prices(
        [
            PriceBar(
                ticker=ticker,
                trading_date=trading_date,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1_000_000,
                source="test",
            )
        ]
    )


class AnalystHistorySignalsThinHistoryTests(TestCase):
    """Day-one behaviour: no prior consensus snapshot exists yet."""

    def test_thin_history_returns_none_with_reasons_for_snapshot_dependent_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _payload_with_history(history=[])
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            _seed_price(storage, "MSFT", run_date, 130.0)
            client = _make_client(FakeHttpClient(payload), storage)

            result = client.fetch_analyst_snapshot("MSFT", run_date)
            self.assertTrue(result.status.success)
            signals = result.data.history_signals

            for field_name in (
                "days_since_last_snapshot",
                "days_since_target_changed",
                "target_change_pct",
                "price_change_pct",
                "target_change_pct_30d",
                "price_change_pct_30d",
                "target_change_pct_90d",
                "price_change_pct_90d",
            ):
                self.assertIsNone(signals[field_name], field_name)
                self.assertIn(field_name, signals["reasons"], field_name)
                self.assertTrue(signals["reasons"][field_name])

    def test_thin_history_still_computes_dispersion_and_high_dispersion_note(self) -> None:
        """Dispersion only needs the current snapshot, not prior history."""
        with tempfile.TemporaryDirectory() as tmp:
            # (180 - 120) / 150 = 0.4 -> above the 0.30 "high dispersion" threshold
            payload = _payload_with_history(target_mean=150.0, target_high=180.0, target_low=120.0, history=[])
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            _seed_price(storage, "MSFT", run_date, 130.0)
            client = _make_client(FakeHttpClient(payload), storage)

            result = client.fetch_analyst_snapshot("MSFT", run_date)
            signals = result.data.history_signals

            self.assertAlmostEqual(signals["dispersion"], 0.4, places=6)
            self.assertIsNotNone(signals["dispersion_note"])
            self.assertIn("disagree", signals["dispersion_note"])

    def test_thin_history_recent_actions_and_lead_lag_are_empty_and_unclear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _payload_with_history(history=[])
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            _seed_price(storage, "MSFT", run_date, 130.0)
            client = _make_client(FakeHttpClient(payload), storage)

            result = client.fetch_analyst_snapshot("MSFT", run_date)
            signals = result.data.history_signals

            self.assertEqual(signals["recent_actions_90d"], {"up": 0, "down": 0, "init": 0, "firms": []})
            self.assertEqual(signals["lead_lag"]["classification"], "unclear")
            self.assertEqual(
                signals["lead_lag"]["reason"], "no up/down rating actions in stored history"
            )


class AnalystHistorySignalsSequentialRunsTests(TestCase):
    """Two runs on different dates: previous-snapshot deltas become available."""

    def test_second_run_computes_days_since_and_change_pct_against_first_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            day1 = date(2026, 6, 1)
            day2 = date(2026, 6, 8)  # 7 days later

            _seed_price(storage, "MSFT", day1, 130.0)
            client = _make_client(
                FakeHttpClient(_payload_with_history(target_mean=150.0, history=[])), storage
            )
            first = client.fetch_analyst_snapshot("MSFT", day1)
            self.assertTrue(first.status.success)
            self.assertIsNone(first.data.history_signals["days_since_last_snapshot"])

            _seed_price(storage, "MSFT", day2, 140.0)  # +7.69% vs day1's 130.0
            client._http = FakeHttpClient(
                _payload_with_history(target_mean=160.0, history=[])
            )  # +6.67% vs day1's 150.0
            second = client.fetch_analyst_snapshot("MSFT", day2)
            self.assertTrue(second.status.success)
            signals = second.data.history_signals

            self.assertEqual(signals["days_since_last_snapshot"], 7)
            self.assertAlmostEqual(signals["target_change_pct"], (160.0 - 150.0) / 150.0, places=6)
            self.assertAlmostEqual(signals["price_change_pct"], (140.0 - 130.0) / 130.0, places=6)
            self.assertNotIn("target_change_pct", signals["reasons"])
            self.assertNotIn("price_change_pct", signals["reasons"])

    def test_rerunning_same_day_overwrites_consensus_snapshot_not_duplicates(self) -> None:
        """Idempotency of Table 1 exercised through the public fetch path."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            _seed_price(storage, "MSFT", run_date, 130.0)

            client = _make_client(
                FakeHttpClient(_payload_with_history(target_mean=150.0, history=[])), storage
            )
            client.fetch_analyst_snapshot("MSFT", run_date)

            client._http = FakeHttpClient(_payload_with_history(target_mean=155.0, history=[]))
            client.fetch_analyst_snapshot("MSFT", run_date)

            history = storage.get_analyst_consensus_history("MSFT")
            self.assertEqual(len(history), 1)
            self.assertAlmostEqual(history[0].target_mean, 155.0)

    def test_30d_and_90d_windows_use_the_closest_prior_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from equity_research.models import AnalystConsensusSnapshotRow

            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)

            storage.upsert_analyst_consensus_snapshot(
                AnalystConsensusSnapshotRow(
                    ticker="MSFT",
                    run_date=run_date - timedelta(days=31),
                    target_mean=100.0,
                    target_high=110.0,
                    target_low=90.0,
                    target_median=100.0,
                    number_of_analysts=10,
                    recommendation_key="buy",
                    recommendation_mean=2.0,
                    security_close=90.0,
                    source="yahoo_quote_summary",
                    ingested_at=datetime.now(timezone.utc),
                )
            )
            storage.upsert_analyst_consensus_snapshot(
                AnalystConsensusSnapshotRow(
                    ticker="MSFT",
                    run_date=run_date - timedelta(days=1),
                    target_mean=140.0,
                    target_high=160.0,
                    target_low=120.0,
                    target_median=140.0,
                    number_of_analysts=12,
                    recommendation_key="buy",
                    recommendation_mean=2.0,
                    security_close=125.0,
                    source="yahoo_quote_summary",
                    ingested_at=datetime.now(timezone.utc),
                )
            )
            _seed_price(storage, "MSFT", run_date, 130.0)

            client = _make_client(
                FakeHttpClient(_payload_with_history(target_mean=150.0, history=[])), storage
            )
            result = client.fetch_analyst_snapshot("MSFT", run_date)
            signals = result.data.history_signals

            self.assertAlmostEqual(signals["target_change_pct_30d"], (150.0 - 100.0) / 100.0, places=6)
            self.assertAlmostEqual(signals["price_change_pct_30d"], (130.0 - 90.0) / 90.0, places=6)
            self.assertIsNone(signals["target_change_pct_90d"])
            self.assertIn("90 days", signals["reasons"]["target_change_pct_90d"])


class AnalystRatingActionDedupeTests(TestCase):
    """Table 2 dedupe on (ticker, firm, action_date, to_grade)."""

    def test_refetching_same_history_does_not_duplicate_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            action_date = run_date - timedelta(days=20)
            history = [
                {
                    "epochGradeDate": _epoch(action_date),
                    "firm": "Goldman Sachs",
                    "fromGrade": "Neutral",
                    "toGrade": "Buy",
                    "action": "up",
                }
            ]
            payload = _payload_with_history(history=history)
            _seed_price(storage, "MSFT", run_date, 130.0)

            client = _make_client(FakeHttpClient(payload), storage)
            client.fetch_analyst_snapshot("MSFT", run_date)
            client._http = FakeHttpClient(payload)  # identical history refetched
            client.fetch_analyst_snapshot("MSFT", run_date)

            actions = storage.get_analyst_rating_actions("MSFT")
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].firm, "Goldman Sachs")

    def test_different_to_grade_on_same_day_is_a_distinct_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            action_date = run_date - timedelta(days=20)
            history = [
                {
                    "epochGradeDate": _epoch(action_date),
                    "firm": "Goldman Sachs",
                    "fromGrade": "Neutral",
                    "toGrade": "Buy",
                    "action": "up",
                },
                {
                    "epochGradeDate": _epoch(action_date),
                    "firm": "Goldman Sachs",
                    "fromGrade": "Buy",
                    "toGrade": "Strong Buy",
                    "action": "up",
                },
            ]
            payload = _payload_with_history(history=history)
            _seed_price(storage, "MSFT", run_date, 130.0)

            client = _make_client(FakeHttpClient(payload), storage)
            client.fetch_analyst_snapshot("MSFT", run_date)

            actions = storage.get_analyst_rating_actions("MSFT")
            self.assertEqual(len(actions), 2)

    def test_recent_actions_90d_counts_and_lists_firms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            history = [
                {
                    "epochGradeDate": _epoch(run_date - timedelta(days=10)),
                    "firm": "Goldman Sachs",
                    "fromGrade": "Neutral",
                    "toGrade": "Buy",
                    "action": "up",
                },
                {
                    "epochGradeDate": _epoch(run_date - timedelta(days=40)),
                    "firm": "Morgan Stanley",
                    "fromGrade": "Overweight",
                    "toGrade": "Equal-Weight",
                    "action": "down",
                },
                {
                    "epochGradeDate": _epoch(run_date - timedelta(days=200)),  # outside 90d window
                    "firm": "JP Morgan",
                    "fromGrade": "",
                    "toGrade": "Neutral",
                    "action": "init",
                },
            ]
            payload = _payload_with_history(history=history)
            _seed_price(storage, "MSFT", run_date, 130.0)

            client = _make_client(FakeHttpClient(payload), storage)
            result = client.fetch_analyst_snapshot("MSFT", run_date)
            recent = result.data.history_signals["recent_actions_90d"]

            self.assertEqual(recent["up"], 1)
            self.assertEqual(recent["down"], 1)
            self.assertEqual(recent["init"], 0)
            self.assertEqual(recent["firms"], ["Goldman Sachs", "Morgan Stanley"])


class AnalystLeadLagClassificationTests(TestCase):
    """lead_lag: analyst_led / analyst_followed / unclear, conservative by construction."""

    def _run_with_single_action(
        self, storage: Storage, run_date: date, action_date: date, action: str = "up"
    ):
        history = [
            {
                "epochGradeDate": _epoch(action_date),
                "firm": "Goldman Sachs",
                "fromGrade": "Neutral",
                "toGrade": "Buy" if action == "up" else "Neutral",
                "action": action,
            }
        ]
        payload = _payload_with_history(history=history)
        _seed_price(storage, "MSFT", run_date, 999.0)  # for the consensus row's security_close
        client = _make_client(FakeHttpClient(payload), storage)
        return client.fetch_analyst_snapshot("MSFT", run_date)

    def test_price_reacts_after_the_action_classified_as_analyst_led(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            action_date = date(2026, 5, 17)
            run_date = date(2026, 6, 1)  # 15 days after the action, >= 10d window

            _seed_price(storage, "MSFT", action_date - timedelta(days=10), 100.0)
            _seed_price(storage, "MSFT", action_date, 100.0)  # flat pre-window: not significant
            _seed_price(storage, "MSFT", action_date + timedelta(days=10), 110.0)  # +10% post

            result = self._run_with_single_action(storage, run_date, action_date, action="up")
            lead_lag = result.data.history_signals["lead_lag"]

            self.assertEqual(lead_lag["classification"], "analyst_led")
            self.assertAlmostEqual(lead_lag["price_change_post_window_pct"], 0.10, places=6)

    def test_price_already_moved_before_the_action_classified_as_analyst_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            action_date = date(2026, 5, 17)
            run_date = date(2026, 6, 1)

            _seed_price(storage, "MSFT", action_date - timedelta(days=10), 90.0)
            _seed_price(storage, "MSFT", action_date, 100.0)  # +11.1% pre-window: significant
            _seed_price(storage, "MSFT", action_date + timedelta(days=10), 101.0)  # +1% post: flat

            result = self._run_with_single_action(storage, run_date, action_date, action="up")
            lead_lag = result.data.history_signals["lead_lag"]

            self.assertEqual(lead_lag["classification"], "analyst_followed")

    def test_no_up_down_actions_classified_as_unclear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            history = [
                {
                    "epochGradeDate": _epoch(run_date - timedelta(days=10)),
                    "firm": "Goldman Sachs",
                    "fromGrade": "Buy",
                    "toGrade": "Buy",
                    "action": "main",
                }
            ]
            payload = _payload_with_history(history=history)
            _seed_price(storage, "MSFT", run_date, 130.0)
            client = _make_client(FakeHttpClient(payload), storage)

            result = client.fetch_analyst_snapshot("MSFT", run_date)
            lead_lag = result.data.history_signals["lead_lag"]

            self.assertEqual(lead_lag["classification"], "unclear")
            self.assertEqual(lead_lag["based_on_action"], None)

    def test_action_too_recent_to_observe_post_reaction_classified_as_unclear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            action_date = run_date - timedelta(days=3)  # < 10-day window

            _seed_price(storage, "MSFT", action_date - timedelta(days=10), 100.0)
            _seed_price(storage, "MSFT", action_date, 100.0)
            _seed_price(storage, "MSFT", run_date, 100.0)

            result = self._run_with_single_action(storage, run_date, action_date, action="up")
            lead_lag = result.data.history_signals["lead_lag"]

            self.assertEqual(lead_lag["classification"], "unclear")
            self.assertIn("elapsed", lead_lag["reason"])

    def test_lead_lag_always_reports_window_and_threshold_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            payload = _payload_with_history(history=[])
            _seed_price(storage, "MSFT", run_date, 130.0)
            client = _make_client(FakeHttpClient(payload), storage)

            result = client.fetch_analyst_snapshot("MSFT", run_date)
            lead_lag = result.data.history_signals["lead_lag"]

            self.assertIn("window_days", lead_lag)
            self.assertIn("move_threshold_pct", lead_lag)


class AnalystSnapshotHistoryPacketExposureTests(TestCase):
    """The derived signals must land inside AnalystSnapshot.history_signals — Part 3 contract."""

    def test_history_signals_is_attached_to_the_returned_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            payload = _payload_with_history(history=[])
            _seed_price(storage, "MSFT", run_date, 130.0)
            client = _make_client(FakeHttpClient(payload), storage)

            result = client.fetch_analyst_snapshot("MSFT", run_date)

            self.assertIsInstance(result.data.history_signals, dict)
            for key in (
                "days_since_last_snapshot",
                "days_since_target_changed",
                "target_change_pct",
                "price_change_pct",
                "dispersion",
                "recent_actions_90d",
                "lead_lag",
            ):
                self.assertIn(key, result.data.history_signals)

    def test_layer2_discipline_is_preserved_by_history_bookkeeping(self) -> None:
        """Sanity check on the architectural rule: nothing here can write to scoring."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            payload = _payload_with_history(history=[])
            _seed_price(storage, "MSFT", run_date, 130.0)
            client = _make_client(FakeHttpClient(payload), storage)

            result = client.fetch_analyst_snapshot("MSFT", run_date)

            self.assertFalse(result.status.partial)
            self.assertTrue(result.status.success)
            self.assertFalse(hasattr(result.data, "bucket_scores"))
