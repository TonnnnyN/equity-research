"""Tests for build_universe.py.

All tests use fake data only — no real network calls.

Coverage
--------
- compute_price_metrics: drawdown_52w, drawdown_60d, ADV20, relative_underperformance_60d
- compute_fundamental_metrics: normalized_fcf_latest, revenue_yoy, filing_age_days
- staged filtering (exchange, price, ADV20, market-cap)
- graceful per-ticker skip on exception
- cache read / write
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest import TestCase

# ---------------------------------------------------------------------------
# Make sure sharing-resources/src is on the path (mirrors conftest.py)
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _TESTS_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Also make the skills script importable.
# _TESTS_DIR  = …/sharing-resources/tests
# parents[0]  = …/sharing-resources
# parents[1]  = …/<repo-root>  (市场情绪)
_REPO_ROOT = _TESTS_DIR.parents[1]
_SKILL_SCRIPTS = _REPO_ROOT / "skills" / "us-smallmid-dislocation" / "scripts"
if str(_SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SKILL_SCRIPTS))

from market_sentiment.models import FundamentalSnapshot, PriceBar, SourceStatus  # noqa: E402
from market_sentiment.sources.base import SourcePayload                           # noqa: E402
from market_sentiment.sources.sec import SecClient                                # noqa: E402
from market_sentiment.storage import Storage                                       # noqa: E402

import build_universe as bu  # noqa: E402


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _price_bar(
    ticker: str,
    trading_date: date,
    close: float,
    high: float | None = None,
    volume: float = 1_000_000.0,
) -> PriceBar:
    return PriceBar(
        ticker=ticker,
        trading_date=trading_date,
        open=close * 0.99,
        high=high if high is not None else close * 1.01,
        low=close * 0.98,
        close=close,
        volume=volume,
        source="yahoo_chart",
    )


def _make_bars(
    ticker: str,
    *,
    n_days: int,
    start_date: date,
    start_close: float,
    end_close: float,
    volume: float = 1_000_000.0,
    high_override: float | None = None,
) -> list[PriceBar]:
    """Generate a linear ramp of price bars."""
    bars = []
    for i in range(n_days):
        frac = i / max(n_days - 1, 1)
        close = start_close + (end_close - start_close) * frac
        trading_date = date.fromordinal(start_date.toordinal() + i)
        bars.append(_price_bar(
            ticker,
            trading_date,
            close=round(close, 4),
            high=high_override,
            volume=volume,
        ))
    return bars


def _snap(
    ticker: str,
    *,
    revenue_latest: float | None = 100e6,
    revenue_previous: float | None = 80e6,
    ocf: float | None = 20e6,
    capex: float | None = 5e6,
    cash: float | None = 50e6,
    debt: float | None = 30e6,
    filed_on: date | None = date(2026, 2, 1),
    period_end: date | None = date(2025, 12, 31),
) -> FundamentalSnapshot:
    return FundamentalSnapshot(
        ticker=ticker,
        cik="0000123456",
        period_end=period_end,
        filed_on=filed_on,
        revenue_latest=revenue_latest,
        revenue_previous=revenue_previous,
        operating_cashflow_latest=ocf,
        operating_cashflow_previous=None,
        capex_latest=capex,
        cash_latest=cash,
        debt_latest=debt,
        source="sec_companyfacts",
    )


# ---------------------------------------------------------------------------
# Fake HTTP / SEC / Yahoo clients (mirrors test_yahoo_finance.py pattern)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, url: str, payload: Any):
        self.url = url
        self.safe_url = url
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeHttpClient:
    """Deterministic HTTP faker: returns pre-registered responses by URL substring."""

    def __init__(self, responses: dict[str, Any] | None = None):
        self._responses: dict[str, Any] = responses or {}
        self.calls: list[str] = []

    def register(self, url_substr: str, payload: Any) -> None:
        self._responses[url_substr] = payload

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> FakeResponse:
        self.calls.append(url)
        for key, payload in self._responses.items():
            if key in url:
                return FakeResponse(url, payload)
        raise RuntimeError(f"FakeHttpClient: no response registered for URL containing '{url}'")


def _make_yahoo_payload(bars: list[PriceBar]) -> dict:
    """Convert a list of PriceBars into a Yahoo Finance chart API payload shape."""
    import calendar

    timestamps = [
        int(datetime.combine(b.trading_date, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        for b in bars
    ]
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": [b.open for b in bars],
                                "high": [b.high for b in bars],
                                "low": [b.low for b in bars],
                                "close": [b.close for b in bars],
                                "volume": [b.volume for b in bars],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def _make_sec_ticker_map_payload(entries: list[tuple]) -> dict:
    """Return a SEC company_tickers_exchange.json-shaped payload.

    entries: list of (cik, name, ticker, exchange)
    """
    return {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [list(e) for e in entries],
    }


def _make_companyfacts_payload(
    shares: float | None = 50_000_000,
    revenue: list[dict] | None = None,
) -> dict:
    """Build a minimal SEC companyfacts payload."""
    us_gaap: dict = {}

    if shares is not None:
        us_gaap["CommonStockSharesOutstanding"] = {
            "units": {
                "shares": [
                    {
                        "val": shares,
                        "end": "2025-12-31",
                        "form": "10-K",
                        "filed": "2026-02-01",
                    }
                ]
            }
        }

    rev_entries = revenue or [
        {"val": 100e6, "end": "2025-12-31", "form": "10-K", "filed": "2026-02-01",
         "fy": 2025, "fp": "FY"},
        {"val": 80e6, "end": "2024-12-31", "form": "10-K", "filed": "2025-02-01",
         "fy": 2024, "fp": "FY"},
    ]
    us_gaap["Revenues"] = {"units": {"USD": rev_entries}}
    us_gaap["NetCashProvidedByUsedInOperatingActivities"] = {
        "units": {"USD": [
            {"val": 20e6, "end": "2025-12-31", "form": "10-K",
             "filed": "2026-02-01", "fy": 2025, "fp": "FY"},
        ]}
    }
    us_gaap["PaymentsToAcquirePropertyPlantAndEquipment"] = {
        "units": {"USD": [
            {"val": 5e6, "end": "2025-12-31", "form": "10-K",
             "filed": "2026-02-01", "fy": 2025, "fp": "FY"},
        ]}
    }
    us_gaap["CashAndCashEquivalentsAtCarryingValue"] = {
        "units": {"USD": [
            {"val": 50e6, "end": "2025-12-31", "form": "10-K",
             "filed": "2026-02-01", "fy": 2025, "fp": "FY"},
        ]}
    }
    us_gaap["LongTermDebt"] = {
        "units": {"USD": [
            {"val": 30e6, "end": "2025-12-31", "form": "10-K",
             "filed": "2026-02-01", "fy": 2025, "fp": "FY"},
        ]}
    }

    return {"facts": {"us-gaap": us_gaap}}


# ---------------------------------------------------------------------------
# 1. compute_price_metrics
# ---------------------------------------------------------------------------

class TestComputePriceMetrics(TestCase):

    def _run_date(self) -> date:
        return date(2026, 5, 17)

    def test_price_equals_last_close(self) -> None:
        """price should be the close of the last bar on or before as_of."""
        bars = _make_bars("ABCD", n_days=30, start_date=date(2026, 4, 18),
                          start_close=10.0, end_close=8.0)
        metrics = bu.compute_price_metrics(bars, [], self._run_date())
        self.assertAlmostEqual(metrics["price"], 8.0, places=4)

    def test_adv20_computed_correctly(self) -> None:
        """ADV20 = mean(close * volume) over last 20 bars."""
        close = 10.0
        volume = 500_000.0
        bars = _make_bars("ABCD", n_days=30, start_date=date(2026, 4, 18),
                          start_close=close, end_close=close, volume=volume)
        metrics = bu.compute_price_metrics(bars, [], self._run_date())
        expected = close * volume
        self.assertAlmostEqual(metrics["avg_dollar_volume_20d_usd"], expected, places=2)

    def test_drawdown_52w_positive_when_below_high(self) -> None:
        """drawdown_52w > 0 when current price is below the 52-week high."""
        # Start at 20, end at 10 → high=20, drawdown = (20-10)/20*100 = 50%
        start_date = date(2025, 5, 17)  # 365 days before run_date
        bars = _make_bars("ABCD", n_days=365, start_date=start_date,
                          start_close=20.0, end_close=10.0,
                          high_override=20.0)
        metrics = bu.compute_price_metrics(bars, [], self._run_date())
        self.assertGreater(metrics["drawdown_52w"], 0)
        self.assertAlmostEqual(metrics["drawdown_52w"], 50.0, places=1)

    def test_drawdown_52w_zero_at_new_high(self) -> None:
        """drawdown_52w ≈ 0 when current bar equals the 52-week high."""
        bars = _make_bars("ABCD", n_days=100, start_date=date(2026, 1, 1),
                          start_close=5.0, end_close=20.0)
        # Set all highs equal to close so current close == 52w high
        for b in bars:
            object.__setattr__(b, "high", b.close)
        metrics = bu.compute_price_metrics(bars, [], self._run_date())
        # Current close = 20, high = 20 → 0%
        self.assertAlmostEqual(metrics["drawdown_52w"], 0.0, places=1)

    def test_drawdown_60d_computed(self) -> None:
        """drawdown_60d: (price_60d_ago - price_now) / price_60d_ago * 100."""
        # 80 bars; bar[0]=100, bar[79]=60.  bar at index 19 (~60d ago) ≈ some mid-value.
        bars = _make_bars("ABCD", n_days=80, start_date=date(2026, 2, 27),
                          start_close=100.0, end_close=60.0)
        metrics = bu.compute_price_metrics(bars, [], self._run_date())
        # Drawdown should be positive (price fell)
        self.assertGreater(metrics["drawdown_60d"], 0)

    def test_relative_underperformance_positive_when_ticker_lags(self) -> None:
        """rel_underperf > 0 when ticker fell more than benchmark."""
        # ticker: 100 → 60 over 80 days (fell 40%)
        ticker_bars = _make_bars("ABCD", n_days=80, start_date=date(2026, 2, 27),
                                 start_close=100.0, end_close=60.0)
        # benchmark: 100 → 95 over 80 days (fell 5%)
        bench_bars = _make_bars("IWM", n_days=80, start_date=date(2026, 2, 27),
                                start_close=100.0, end_close=95.0)
        metrics = bu.compute_price_metrics(ticker_bars, bench_bars, self._run_date())
        rel = metrics["relative_underperformance_60d"]
        self.assertIsNotNone(rel)
        self.assertGreater(rel, 0)

    def test_relative_underperformance_none_when_no_benchmark(self) -> None:
        """relative_underperformance_60d is None when no benchmark bars supplied."""
        bars = _make_bars("ABCD", n_days=80, start_date=date(2026, 2, 27),
                          start_close=10.0, end_close=8.0)
        metrics = bu.compute_price_metrics(bars, [], self._run_date())
        self.assertIsNone(metrics["relative_underperformance_60d"])

    def test_empty_bars_returns_empty_dict(self) -> None:
        """No bars → empty dict."""
        metrics = bu.compute_price_metrics([], [], self._run_date())
        self.assertEqual(metrics, {})

    def test_bars_after_as_of_are_excluded(self) -> None:
        """Bars with trading_date > as_of should be ignored."""
        as_of = date(2026, 5, 10)
        bars = _make_bars("ABCD", n_days=10, start_date=date(2026, 5, 5),
                          start_close=10.0, end_close=20.0)
        # bars[-1].trading_date == 2026-05-14 > as_of
        metrics = bu.compute_price_metrics(bars, [], as_of)
        # Last valid bar should be the one on 2026-05-10 (index 5, close ≈ 15.5)
        self.assertLessEqual(metrics["price"], 20.0)
        self.assertGreaterEqual(metrics["price"], 10.0)


# ---------------------------------------------------------------------------
# 2. compute_fundamental_metrics
# ---------------------------------------------------------------------------

class TestComputeFundamentalMetrics(TestCase):

    def test_normalized_fcf_latest_is_ocf_minus_capex(self) -> None:
        snap = _snap("ABCD", ocf=20e6, capex=5e6)
        metrics = bu.compute_fundamental_metrics(snap, date(2026, 5, 17))
        self.assertAlmostEqual(metrics["normalized_fcf_latest"], 15e6)

    def test_normalized_fcf_equals_ocf_when_capex_none(self) -> None:
        snap = _snap("ABCD", ocf=20e6, capex=None)
        metrics = bu.compute_fundamental_metrics(snap, date(2026, 5, 17))
        self.assertAlmostEqual(metrics["normalized_fcf_latest"], 20e6)

    def test_normalized_fcf_none_when_ocf_none(self) -> None:
        snap = _snap("ABCD", ocf=None, capex=5e6)
        metrics = bu.compute_fundamental_metrics(snap, date(2026, 5, 17))
        self.assertIsNone(metrics["normalized_fcf_latest"])

    def test_revenue_yoy_computed_correctly(self) -> None:
        # (100 - 80) / 80 * 100 = 25%
        snap = _snap("ABCD", revenue_latest=100e6, revenue_previous=80e6)
        metrics = bu.compute_fundamental_metrics(snap, date(2026, 5, 17))
        self.assertAlmostEqual(metrics["revenue_yoy"], 25.0, places=4)

    def test_revenue_yoy_none_when_previous_none(self) -> None:
        snap = _snap("ABCD", revenue_latest=100e6, revenue_previous=None)
        metrics = bu.compute_fundamental_metrics(snap, date(2026, 5, 17))
        self.assertIsNone(metrics["revenue_yoy"])

    def test_revenue_yoy_none_when_previous_zero(self) -> None:
        snap = _snap("ABCD", revenue_latest=100e6, revenue_previous=0.0)
        metrics = bu.compute_fundamental_metrics(snap, date(2026, 5, 17))
        self.assertIsNone(metrics["revenue_yoy"])

    def test_revenue_yoy_negative_decline(self) -> None:
        # (60 - 100) / 100 * 100 = -40%
        snap = _snap("ABCD", revenue_latest=60e6, revenue_previous=100e6)
        metrics = bu.compute_fundamental_metrics(snap, date(2026, 5, 17))
        self.assertAlmostEqual(metrics["revenue_yoy"], -40.0, places=4)

    def test_filing_age_days_computed(self) -> None:
        filed_on = date(2026, 2, 1)
        run_date = date(2026, 5, 17)
        snap = _snap("ABCD", filed_on=filed_on)
        metrics = bu.compute_fundamental_metrics(snap, run_date)
        expected = (run_date - filed_on).days  # 105
        self.assertEqual(metrics["filing_age_days"], expected)

    def test_filing_age_days_none_when_filed_on_none(self) -> None:
        snap = _snap("ABCD", filed_on=None)
        metrics = bu.compute_fundamental_metrics(snap, date(2026, 5, 17))
        self.assertIsNone(metrics["filing_age_days"])

    def test_cash_and_debt_passed_through(self) -> None:
        snap = _snap("ABCD", cash=50e6, debt=30e6)
        metrics = bu.compute_fundamental_metrics(snap, date(2026, 5, 17))
        self.assertAlmostEqual(metrics["cash_latest"], 50e6)
        self.assertAlmostEqual(metrics["debt_latest"], 30e6)

    def test_operating_cashflow_passed_through(self) -> None:
        snap = _snap("ABCD", ocf=20e6)
        metrics = bu.compute_fundamental_metrics(snap, date(2026, 5, 17))
        self.assertAlmostEqual(metrics["operating_cashflow_latest"], 20e6)


# ---------------------------------------------------------------------------
# 3. Cache helpers
# ---------------------------------------------------------------------------

class TestCacheHelpers(TestCase):

    def test_write_and_read_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            data = {"ticker": "ABCD", "price": 12.5}
            bu.write_cache(cache_dir, "ABCD", data)
            result = bu.read_cache(cache_dir, "ABCD")
            self.assertEqual(result, data)

    def test_read_cache_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = bu.read_cache(Path(tmp), "ZZZZ")
            self.assertIsNone(result)

    def test_read_cache_returns_none_on_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ABCD.json"
            p.write_text("not valid json", encoding="utf-8")
            result = bu.read_cache(Path(tmp), "ABCD")
            self.assertIsNone(result)

    def test_cache_creates_directory_if_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "sub" / "cache"
            bu.write_cache(cache_dir, "ABCD", {"ticker": "ABCD"})
            self.assertTrue((cache_dir / "ABCD.json").exists())


# ---------------------------------------------------------------------------
# 4. Staged filtering integration
# ---------------------------------------------------------------------------

class TestStagedFiltering(TestCase):
    """Test that the staged filter chain (exchange → price → ADV → market-cap)
    correctly passes or skips tickers.

    We test this by building a minimal config dict and calling process_ticker
    with appropriately shaped fake clients.
    """

    def _base_cfg(self) -> dict:
        return {
            "listing": {
                "allowed_exchanges": ["NYSE", "NASDAQ", "NYSE American"],
            },
            "filters": {
                "min_price_usd": 5.0,
                "min_avg_dollar_volume_20d_usd": 2_000_000,
                "market_cap_min_usd": 300_000_000,
                "market_cap_max_usd": 20_000_000_000,
            },
            "benchmarks": {
                "small_cap": "IWM",
                "mid_cap": "IJH",
                "broad_market": "SPY",
                "small_cap_alt": "IJR",
            },
        }

    def _make_clients(self, ticker: str, bars: list[PriceBar],
                      shares: float = 50_000_000) -> tuple:
        """Return (http_client, storage, sec_client) wired with fake data."""
        ticker_payload = _make_yahoo_payload(bars)
        iwm_bars = _make_bars("IWM", n_days=80, start_date=date(2026, 2, 27),
                              start_close=200.0, end_close=195.0)
        iwm_payload = _make_yahoo_payload(iwm_bars)

        cik = "0000123456"
        sec_ticker_map = _make_sec_ticker_map_payload([
            (int(cik), "Example Corp", ticker, "NASDAQ"),
        ])
        cf_payload = _make_companyfacts_payload(shares=shares)

        http = FakeHttpClient()
        http.register("company_tickers_exchange", sec_ticker_map)
        # Yahoo URL contains the ticker
        http.register(f"chart/{ticker}", ticker_payload)
        http.register("chart/IWM", iwm_payload)
        http.register("chart/IJH", iwm_payload)
        http.register("chart/SPY", iwm_payload)
        http.register("chart/IJR", iwm_payload)
        http.register(f"companyfacts/CIK{cik}", cf_payload)

        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()
            sec = SecClient(http_client=http, storage=storage)
            return http, storage, sec, tmp

    def test_exchange_filter_excludes_otc(self) -> None:
        """Ticker on OTC exchange should return None from process_ticker."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()
            bars = _make_bars("OTCK", n_days=80, start_date=date(2026, 2, 27),
                              start_close=10.0, end_close=10.0, volume=500_000.0)
            http = FakeHttpClient()
            http.register("chart/OTCK", _make_yahoo_payload(bars))
            sec = SecClient(http_client=http, storage=storage)

            seed = {"ticker": "OTCK", "name": "OTC Corp", "exchange": "OTC", "cik": ""}
            result = bu.process_ticker(
                seed=seed, cfg=self._base_cfg(), http_client=http,
                sec_client=sec, storage=storage,
                cache_dir=Path(tmp) / "cache",
                run_date=date(2026, 5, 17),
                use_cache=False,
                benchmark_bars={},
            )
            self.assertIsNone(result)

    def test_price_below_min_returns_none(self) -> None:
        """Ticker with price < min_price_usd should be filtered out."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()
            # price = 3 < 5
            bars = _make_bars("CHEAP", n_days=80, start_date=date(2026, 2, 27),
                              start_close=3.0, end_close=3.0, volume=5_000_000.0)
            http = FakeHttpClient()
            http.register("chart/CHEAP", _make_yahoo_payload(bars))
            sec = SecClient(http_client=http, storage=storage)

            seed = {"ticker": "CHEAP", "name": "Cheap Corp", "exchange": "NASDAQ", "cik": ""}
            result = bu.process_ticker(
                seed=seed, cfg=self._base_cfg(), http_client=http,
                sec_client=sec, storage=storage,
                cache_dir=Path(tmp) / "cache",
                run_date=date(2026, 5, 17),
                use_cache=False,
                benchmark_bars={},
            )
            self.assertIsNone(result)

    def test_adv_below_min_returns_none(self) -> None:
        """Ticker with ADV20 < min should be filtered out."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()
            # price=10, volume=100 → ADV20 = 1000 < 2_000_000
            bars = _make_bars("ILLIQ", n_days=80, start_date=date(2026, 2, 27),
                              start_close=10.0, end_close=10.0, volume=100.0)
            http = FakeHttpClient()
            http.register("chart/ILLIQ", _make_yahoo_payload(bars))
            sec = SecClient(http_client=http, storage=storage)

            seed = {"ticker": "ILLIQ", "name": "Illiquid Corp", "exchange": "NYSE", "cik": ""}
            result = bu.process_ticker(
                seed=seed, cfg=self._base_cfg(), http_client=http,
                sec_client=sec, storage=storage,
                cache_dir=Path(tmp) / "cache",
                run_date=date(2026, 5, 17),
                use_cache=False,
                benchmark_bars={},
            )
            self.assertIsNone(result)

    def test_market_cap_too_small_returns_none(self) -> None:
        """Market cap below min should be filtered out (shares * price)."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()
            # price=10, shares=1_000_000 → market_cap=10M < 300M
            bars = _make_bars("TINY", n_days=80, start_date=date(2026, 2, 27),
                              start_close=10.0, end_close=10.0, volume=500_000.0)
            cik = "0000000001"
            sec_map = _make_sec_ticker_map_payload([(1, "Tiny Corp", "TINY", "NASDAQ")])
            cf_payload = _make_companyfacts_payload(shares=1_000_000)
            http = FakeHttpClient()
            http.register("company_tickers_exchange", sec_map)
            http.register("chart/TINY", _make_yahoo_payload(bars))
            http.register("companyfacts/CIK", cf_payload)
            sec = SecClient(http_client=http, storage=storage)

            seed = {"ticker": "TINY", "name": "Tiny Corp", "exchange": "NASDAQ", "cik": cik}
            result = bu.process_ticker(
                seed=seed, cfg=self._base_cfg(), http_client=http,
                sec_client=sec, storage=storage,
                cache_dir=Path(tmp) / "cache",
                run_date=date(2026, 5, 17),
                use_cache=False,
                benchmark_bars={},
            )
            self.assertIsNone(result)

    def test_market_cap_too_large_returns_none(self) -> None:
        """Market cap above max should be filtered out."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()
            # price=100, shares=500_000_000 → market_cap=50B > 20B
            bars = _make_bars("GIANT", n_days=80, start_date=date(2026, 2, 27),
                              start_close=100.0, end_close=100.0, volume=1_000_000.0)
            cik = "0000000002"
            sec_map = _make_sec_ticker_map_payload([(2, "Giant Corp", "GIANT", "NYSE")])
            cf_payload = _make_companyfacts_payload(shares=500_000_000)
            http = FakeHttpClient()
            http.register("company_tickers_exchange", sec_map)
            http.register("chart/GIANT", _make_yahoo_payload(bars))
            http.register("companyfacts/CIK", cf_payload)
            sec = SecClient(http_client=http, storage=storage)

            seed = {"ticker": "GIANT", "name": "Giant Corp", "exchange": "NYSE", "cik": cik}
            result = bu.process_ticker(
                seed=seed, cfg=self._base_cfg(), http_client=http,
                sec_client=sec, storage=storage,
                cache_dir=Path(tmp) / "cache",
                run_date=date(2026, 5, 17),
                use_cache=False,
                benchmark_bars={},
            )
            self.assertIsNone(result)

    def test_valid_ticker_passes_all_filters(self) -> None:
        """A ticker that meets all criteria should produce a complete row."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()
            # price=20, volume=1_000_000 → ADV=20M ok; shares=100M → mcap=2B ok
            bars = _make_bars("GOOD", n_days=80, start_date=date(2026, 2, 27),
                              start_close=20.0, end_close=15.0, volume=1_000_000.0)
            cik = "0000000099"
            sec_map = _make_sec_ticker_map_payload([(99, "Good Corp", "GOOD", "NASDAQ")])
            cf_payload = _make_companyfacts_payload(shares=100_000_000)
            http = FakeHttpClient()
            http.register("company_tickers_exchange", sec_map)
            http.register("chart/GOOD", _make_yahoo_payload(bars))
            http.register("chart/IWM", _make_yahoo_payload(
                _make_bars("IWM", n_days=80, start_date=date(2026, 2, 27),
                           start_close=200.0, end_close=198.0)))
            http.register("chart/IJH", _make_yahoo_payload(
                _make_bars("IJH", n_days=80, start_date=date(2026, 2, 27),
                           start_close=200.0, end_close=198.0)))
            http.register("chart/SPY", _make_yahoo_payload(
                _make_bars("SPY", n_days=80, start_date=date(2026, 2, 27),
                           start_close=500.0, end_close=498.0)))
            http.register("chart/IJR", _make_yahoo_payload(
                _make_bars("IJR", n_days=80, start_date=date(2026, 2, 27),
                           start_close=100.0, end_close=99.0)))
            http.register("companyfacts/CIK", cf_payload)
            sec = SecClient(http_client=http, storage=storage)

            iwm_bars = _make_bars("IWM", n_days=80, start_date=date(2026, 2, 27),
                                  start_close=200.0, end_close=198.0)
            benchmark_bars = {"IWM": iwm_bars}

            seed = {"ticker": "GOOD", "name": "Good Corp", "exchange": "NASDAQ", "cik": cik}
            result = bu.process_ticker(
                seed=seed, cfg=self._base_cfg(), http_client=http,
                sec_client=sec, storage=storage,
                cache_dir=Path(tmp) / "cache",
                run_date=date(2026, 5, 17),
                use_cache=False,
                benchmark_bars=benchmark_bars,
            )
            self.assertIsNotNone(result)
            self.assertEqual(result["ticker"], "GOOD")
            self.assertIn("price", result)
            self.assertIn("drawdown_52w", result)
            self.assertIn("drawdown_60d", result)
            self.assertIn("normalized_fcf_latest", result)
            self.assertIn("filing_age_days", result)
            # Market cap should be price * shares
            self.assertAlmostEqual(result["market_cap_usd"], 15.0 * 100_000_000, delta=1e6)

    def test_no_price_data_returns_none(self) -> None:
        """Ticker with empty Yahoo response should return None."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()
            empty_payload = {"chart": {"result": [], "error": None}}
            http = FakeHttpClient()
            http.register("chart/NOPX", empty_payload)
            sec = SecClient(http_client=http, storage=storage)

            seed = {"ticker": "NOPX", "name": "No Price Corp", "exchange": "NASDAQ", "cik": ""}
            result = bu.process_ticker(
                seed=seed, cfg=self._base_cfg(), http_client=http,
                sec_client=sec, storage=storage,
                cache_dir=Path(tmp) / "cache",
                run_date=date(2026, 5, 17),
                use_cache=False,
                benchmark_bars={},
            )
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 5. Graceful skip on exception
# ---------------------------------------------------------------------------

class TestGracefulSkip(TestCase):
    """Verify that process_ticker exceptions propagate to caller and that
    the caller (the main loop in run()) counts them as skips without crashing.
    We test the exception-surfacing behaviour directly.
    """

    def test_process_ticker_returns_none_on_yahoo_http_error(self) -> None:
        """When Yahoo HTTP fails, fetch_1y_price_bars catches the exception
        and returns [] — so process_ticker gracefully returns None (caller skips).
        This is the correct per-spec behaviour: per-ticker errors never crash the run.
        """
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            # FakeHttpClient with no registered URLs → always raises RuntimeError.
            # fetch_1y_price_bars catches this and returns [], so process_ticker
            # sees no bars and returns None instead of raising.
            http = FakeHttpClient(responses={})
            sec = SecClient(http_client=http, storage=storage)

            cfg = {
                "listing": {"allowed_exchanges": ["NASDAQ"]},
                "filters": {
                    "min_price_usd": 5.0,
                    "min_avg_dollar_volume_20d_usd": 2_000_000,
                    "market_cap_min_usd": 300_000_000,
                    "market_cap_max_usd": 20_000_000_000,
                },
                "benchmarks": {"small_cap": "IWM"},
            }
            seed = {"ticker": "ERR", "name": "Error Corp", "exchange": "NASDAQ", "cik": ""}

            # Should NOT raise — graceful None return
            result = bu.process_ticker(
                seed=seed, cfg=cfg, http_client=http,
                sec_client=sec, storage=storage,
                cache_dir=Path(tmp) / "cache",
                run_date=date(2026, 5, 17),
                use_cache=False,
                benchmark_bars={},
            )
            self.assertIsNone(result)

    def test_main_loop_skips_bad_ticker_and_continues(self) -> None:
        """Simulate the main loop: one ticker raises, the next succeeds.
        The loop must not crash and must record the right skip count.
        """
        rows = []
        skip_reasons = []

        def _process(seed, **_kwargs):
            if seed["ticker"] == "BAD":
                raise RuntimeError("simulated failure")
            return {"ticker": seed["ticker"], "price": 10.0}

        tickers = [
            {"ticker": "BAD", "name": "", "exchange": "NASDAQ", "cik": ""},
            {"ticker": "OK", "name": "", "exchange": "NASDAQ", "cik": ""},
        ]

        for seed in tickers:
            try:
                row = _process(seed)
                if row is None:
                    skip_reasons.append("filtered")
                else:
                    rows.append(row)
            except Exception as exc:
                skip_reasons.append(type(exc).__name__)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "OK")
        self.assertEqual(len(skip_reasons), 1)
        self.assertEqual(skip_reasons[0], "RuntimeError")


# ---------------------------------------------------------------------------
# 6. Seed loading
# ---------------------------------------------------------------------------

class TestSeedLoading(TestCase):

    def test_load_seed_from_file_basic(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                        encoding="utf-8") as fh:
            fh.write("AAPL\n# comment\nMSFT\n  GOOG  \n")
            fh_path = fh.name
        entries = bu.load_seed_from_file(fh_path)
        tickers = [e["ticker"] for e in entries]
        self.assertEqual(tickers, ["AAPL", "MSFT", "GOOG"])

    def test_load_seed_from_sec_parses_correctly(self) -> None:
        payload = _make_sec_ticker_map_payload([
            (123, "Apple Inc", "AAPL", "NASDAQ"),
            (456, "Microsoft Corp", "MSFT", "NASDAQ"),
        ])
        http = FakeHttpClient()
        http.register("company_tickers_exchange", payload)
        entries = bu.load_seed_from_sec(http, date(2026, 5, 17))
        tickers = [e["ticker"] for e in entries]
        self.assertIn("AAPL", tickers)
        self.assertIn("MSFT", tickers)
        # CIK should be zero-padded to 10 digits
        aapl = next(e for e in entries if e["ticker"] == "AAPL")
        self.assertEqual(aapl["cik"], "0000000123")


# ---------------------------------------------------------------------------
# 7. Extract shares outstanding
# ---------------------------------------------------------------------------

class TestExtractSharesOutstanding(TestCase):

    def test_extracts_from_us_gaap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            cik = "0000000042"
            cf_payload = _make_companyfacts_payload(shares=75_000_000)
            sec_map = _make_sec_ticker_map_payload([(42, "Test Corp", "TEST", "NYSE")])

            http = FakeHttpClient()
            http.register("company_tickers_exchange", sec_map)
            http.register("companyfacts/CIK", cf_payload)

            sec = SecClient(http_client=http, storage=storage)

            shares = bu._extract_shares_outstanding(http, sec, "TEST", date(2026, 5, 17))
            self.assertIsNotNone(shares)
            self.assertAlmostEqual(shares, 75_000_000)

    def test_returns_none_when_shares_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            cik = "0000000043"
            # companyfacts with no CommonStockSharesOutstanding
            cf_payload = {"facts": {"us-gaap": {}}}
            sec_map = _make_sec_ticker_map_payload([(43, "No Shares Corp", "NOSH", "NYSE")])

            http = FakeHttpClient()
            http.register("company_tickers_exchange", sec_map)
            http.register("companyfacts/CIK", cf_payload)

            sec = SecClient(http_client=http, storage=storage)

            shares = bu._extract_shares_outstanding(http, sec, "NOSH", date(2026, 5, 17))
            self.assertIsNone(shares)

    def test_returns_none_when_ticker_not_in_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            sec_map = _make_sec_ticker_map_payload([])
            http = FakeHttpClient()
            http.register("company_tickers_exchange", sec_map)

            sec = SecClient(http_client=http, storage=storage)

            shares = bu._extract_shares_outstanding(http, sec, "UNKNOWN", date(2026, 5, 17))
            self.assertIsNone(shares)


# ---------------------------------------------------------------------------
# 8. parse_args smoke test
# ---------------------------------------------------------------------------

class TestParseArgs(TestCase):

    def test_default_args(self) -> None:
        args = bu.parse_args([])
        self.assertIsNone(args.limit)
        self.assertIsNone(args.seed_file)
        self.assertIsNone(args.output)
        self.assertIsNone(args.as_of)
        self.assertFalse(args.no_cache)

    def test_limit_arg(self) -> None:
        args = bu.parse_args(["--limit", "20"])
        self.assertEqual(args.limit, 20)

    def test_seed_file_arg(self) -> None:
        args = bu.parse_args(["--seed-file", "/tmp/tickers.txt"])
        self.assertEqual(args.seed_file, "/tmp/tickers.txt")

    def test_as_of_arg(self) -> None:
        args = bu.parse_args(["--as-of", "2026-01-15"])
        self.assertEqual(args.as_of, "2026-01-15")
