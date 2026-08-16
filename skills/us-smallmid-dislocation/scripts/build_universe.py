#!/usr/bin/env python3
"""build_universe.py — Stage-0 universe construction for us-smallmid-dislocation.

Assembles the CSV that screen_candidates.py consumes.

Pipeline
--------
1. Load seed ticker list (SEC company_tickers_exchange.json or --seed-file).
2. Filter by allowed_exchanges from universe.toml.
3. Fetch Yahoo daily prices (1 year).  Apply cheap filters:
       price >= min_price_usd
       ADV20 >= min_avg_dollar_volume_20d_usd
4. Compute provisional market_cap using shares-outstanding from SEC companyfacts.
   Apply market-cap band filter.
5. Fetch SEC companyfacts for survivors; derive fundamental columns.
6. Write universe CSV.

All per-ticker results are cached under data/state/universe_cache/<TICKER>.json.
Re-runs skip already-cached tickers.

Usage
-----
    python3 build_universe.py [--limit N] [--seed-file PATH] [--output PATH]
                               [--as-of YYYY-MM-DD] [--cache-dir PATH]
                               [--config PATH]

Requires Python >= 3.11 (tomllib is stdlib).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
import tomllib
import warnings
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Locate the shared library that lives next to this skill tree.
# Directory layout:
#   skills/us-smallmid-dislocation/scripts/build_universe.py   ← this file
#   sharing-resources/src/market_sentiment/                     ← shared lib
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPTS_DIR.parent          # skills/us-smallmid-dislocation/
_REPO_ROOT = _SKILL_ROOT.parents[1]        # repo root
_SHARING_SRC = _REPO_ROOT / "sharing-resources" / "src"
if _SHARING_SRC.exists() and str(_SHARING_SRC) not in sys.path:
    sys.path.insert(0, str(_SHARING_SRC))

from market_sentiment.http import HttpClient                            # noqa: E402
from market_sentiment.models import FundamentalSnapshot, PriceBar      # noqa: E402
from market_sentiment.sources.sec import SecClient, _build_snapshot    # noqa: E402
from market_sentiment.sources.yahoo_finance import YahooFinanceClient  # noqa: E402
from market_sentiment.storage import Storage                            # noqa: E402

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG = _SKILL_ROOT / "defaults" / "universe.toml"
_DEFAULT_CACHE_DIR = _REPO_ROOT / "data" / "state" / "universe_cache"
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"
_DEFAULT_DB_PATH = _REPO_ROOT / "data" / "market_sentiment.db"

SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
USER_AGENT = "build_universe/1.0 research@example.com"

# Yahoo provides ~6 months by default; we need ~1 year for 52-week stats.
# We fetch twice: once for the 1-year range (used for drawdown_52w) and once
# for the 6-month range the YahooFinanceClient already uses for ADV/drawdown_60d.
# Rather than duplicating client calls we pass range="1y" via a patched call.
YAHOO_1Y_PARAMS = {
    "interval": "1d",
    "range": "1y",
    "events": "div,splits",
    "includePrePost": "false",
}

# SEC rate-limit: 10 req/s.  We sleep briefly between calls.
SEC_RATE_LIMIT_INTERVAL = 0.11  # seconds between SEC requests

# Output CSV columns in schema order (matches input_schema.md).
OUTPUT_COLUMNS = [
    "ticker", "name", "exchange", "cik",
    "market_cap_usd", "price", "avg_dollar_volume_20d_usd",
    "drawdown_52w", "drawdown_60d", "relative_underperformance_60d",
    # fundamental stability
    "revenue_yoy", "operating_cashflow_latest", "normalized_fcf_latest",
    "cash_latest", "debt_latest", "filing_age_days",
    # strongly-recommended (left blank — screen_candidates.py is None-safe)
    "country", "security_type", "structure", "sector", "industry", "benchmark", "flags",
    # runway/interest/share-count (blank — not derivable without paid data)
    "runway_months", "interest_coverage", "share_count_growth_yoy",
    "working_capital_release_ratio",
    # event/tape (blank)
    "earnings_days_ago", "earnings_quality", "earnings_price_confirmation",
    "post_earnings_relative_return_5d",
]

log = logging.getLogger("build_universe")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a universe CSV for screen_candidates.py.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=str(_DEFAULT_CONFIG),
        help="Path to universe.toml config file.",
    )
    parser.add_argument(
        "--seed-file",
        default=None,
        help="Path to a plain-text file with one ticker per line.  "
             "Skips the SEC company_tickers_exchange.json seed.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output CSV path.  Default: data/universe/universe_<date>.csv",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        dest="as_of",
        help="Reference date for drawdown calculations (YYYY-MM-DD).  Default: today.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N tickers (useful for smoke-testing).",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(_DEFAULT_CACHE_DIR),
        help="Directory for per-ticker JSON cache files.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore and overwrite existing cache entries.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------------------
# Seed ticker list
# ---------------------------------------------------------------------------

def load_seed_from_file(path: str) -> list[dict]:
    """Load tickers from a plain-text file (one ticker per line).

    Returns a list of dicts with keys: ticker, name, exchange, cik.
    Fields other than ticker are left blank.
    """
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            ticker = line.strip().upper()
            if ticker and not ticker.startswith("#"):
                entries.append({
                    "ticker": ticker,
                    "name": "",
                    "exchange": "",
                    "cik": "",
                })
    return entries


def load_seed_from_sec(http_client: HttpClient, run_date: date) -> list[dict]:
    """Fetch SEC company_tickers_exchange.json and return parsed entries.

    Returns a list of dicts with keys: ticker, name, exchange, cik.
    The SEC JSON has the form:
        {"fields": [...], "data": [[cik, name, ticker, exchange], ...]}
    """
    log.info("Fetching SEC seed list: %s", SEC_TICKER_MAP_URL)
    response = http_client.get(SEC_TICKER_MAP_URL)
    payload = response.json()
    fields = payload.get("fields", [])
    data = payload.get("data", [])

    # Determine field positions (they may vary in future SEC releases).
    try:
        idx_cik = fields.index("cik")
        idx_name = fields.index("name")
        idx_ticker = fields.index("ticker")
        idx_exchange = fields.index("exchange")
    except ValueError:
        # Fallback: assume the documented order [cik, name, ticker, exchange]
        idx_cik, idx_name, idx_ticker, idx_exchange = 0, 1, 2, 3

    entries = []
    for row in data:
        if len(row) <= max(idx_cik, idx_name, idx_ticker, idx_exchange):
            continue
        ticker = str(row[idx_ticker]).upper().strip()
        if not ticker:
            continue
        cik_raw = row[idx_cik]
        cik = str(int(cik_raw)).zfill(10) if cik_raw is not None else ""
        entries.append({
            "ticker": ticker,
            "name": str(row[idx_name]).strip(),
            "exchange": str(row[idx_exchange]).strip(),
            "cik": cik,
        })
    log.info("SEC seed: %d tickers", len(entries))
    return entries


# ---------------------------------------------------------------------------
# Price-based metrics computation
# ---------------------------------------------------------------------------

def _trading_days_ago(bars: list[PriceBar], n: int) -> PriceBar | None:
    """Return the bar approximately n trading-days before the last bar."""
    if len(bars) < 2:
        return None
    # bars is sorted ascending
    target_index = max(0, len(bars) - 1 - n)
    return bars[target_index]


def compute_price_metrics(
    ticker_bars: list[PriceBar],
    benchmark_bars: list[PriceBar],
    as_of: date,
) -> dict[str, Any]:
    """Compute price, ADV20, drawdown_52w, drawdown_60d, relative_underperformance_60d.

    Parameters
    ----------
    ticker_bars:    Sorted-ascending PriceBar list for the candidate ticker.
    benchmark_bars: Sorted-ascending PriceBar list for the benchmark (IWM/IJH).
    as_of:          Reference date (today or --as-of arg).

    Returns
    -------
    dict with keys:
        price, avg_dollar_volume_20d_usd,
        drawdown_52w, drawdown_60d, relative_underperformance_60d
    All ratios are expressed as positive percentages (e.g. 25 = 25% drawdown).
    """
    # Filter bars up to as_of date
    bars = [b for b in ticker_bars if b.trading_date <= as_of]
    if not bars:
        return {}

    current = bars[-1]
    price = current.close

    # ADV20: average(close × volume) over last 20 bars
    recent_20 = bars[-20:]
    dollar_volumes = [
        b.close * b.volume
        for b in recent_20
        if b.volume is not None and b.volume > 0
    ]
    avg_dollar_volume_20d = sum(dollar_volumes) / len(dollar_volumes) if dollar_volumes else 0.0

    # drawdown_52w: (52w_high - current) / 52w_high × 100
    bars_52w = [b for b in bars if (as_of - b.trading_date).days <= 365]
    if bars_52w:
        high_52w = max(b.high for b in bars_52w)
        drawdown_52w = ((high_52w - price) / high_52w * 100) if high_52w > 0 else 0.0
    else:
        drawdown_52w = 0.0

    # drawdown_60d: (60d_ago_close - current) / 60d_ago_close × 100
    bar_60d_ago = _trading_days_ago(bars, 60)
    if bar_60d_ago and bar_60d_ago.close > 0:
        drawdown_60d = ((bar_60d_ago.close - price) / bar_60d_ago.close * 100)
    else:
        drawdown_60d = 0.0

    # relative_underperformance_60d: ticker 60d return − benchmark 60d return (pct)
    bench = [b for b in benchmark_bars if b.trading_date <= as_of]
    rel_underperf = None
    if bench and bar_60d_ago and bar_60d_ago.close > 0:
        bench_now = bench[-1].close
        bench_60d_bar = _trading_days_ago(bench, 60)
        if bench_60d_bar and bench_60d_bar.close > 0:
            ticker_ret_60d = (price - bar_60d_ago.close) / bar_60d_ago.close * 100
            bench_ret_60d = (bench_now - bench_60d_bar.close) / bench_60d_bar.close * 100
            # Positive = ticker underperformed (ticker lost more / gained less)
            rel_underperf = bench_ret_60d - ticker_ret_60d

    return {
        "price": price,
        "avg_dollar_volume_20d_usd": avg_dollar_volume_20d,
        "drawdown_52w": drawdown_52w,
        "drawdown_60d": drawdown_60d,
        "relative_underperformance_60d": rel_underperf,
    }


# ---------------------------------------------------------------------------
# Fundamental-column derivation
# ---------------------------------------------------------------------------

def compute_fundamental_metrics(
    snap: FundamentalSnapshot,
    run_date: date,
) -> dict[str, Any]:
    """Derive fundamental columns from a FundamentalSnapshot.

    Returns a dict; missing values are None (screen_candidates.py is None-safe).
    """
    # normalized_fcf_latest = operating_cashflow - capex
    ocf = snap.operating_cashflow_latest
    capex = snap.capex_latest  # already abs() in SecClient._build_snapshot
    if ocf is not None and capex is not None:
        normalized_fcf = ocf - capex
    elif ocf is not None:
        normalized_fcf = ocf
    else:
        normalized_fcf = None

    # revenue_yoy = (rev_latest - rev_previous) / rev_previous
    rev_lat = snap.revenue_latest
    rev_prev = snap.revenue_previous
    if rev_lat is not None and rev_prev is not None and rev_prev != 0:
        revenue_yoy = (rev_lat - rev_prev) / abs(rev_prev) * 100
    else:
        revenue_yoy = None

    # filing_age_days = run_date - filed_on
    if snap.filed_on is not None:
        filing_age_days = (run_date - snap.filed_on).days
    else:
        filing_age_days = None

    return {
        "revenue_yoy": revenue_yoy,
        "operating_cashflow_latest": ocf,
        "normalized_fcf_latest": normalized_fcf,
        "cash_latest": snap.cash_latest,
        "debt_latest": snap.debt_latest,
        "filing_age_days": filing_age_days,
        "cik": snap.cik or "",
    }


# ---------------------------------------------------------------------------
# Per-ticker cache helpers
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: Path, ticker: str) -> Path:
    return cache_dir / f"{ticker.upper()}.json"


def read_cache(cache_dir: Path, ticker: str) -> dict | None:
    path = _cache_path(cache_dir, ticker)
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None
    return None


def write_cache(cache_dir: Path, ticker: str, data: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, ticker)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# Single-ticker processing
# ---------------------------------------------------------------------------

def fetch_1y_price_bars(
    http_client: HttpClient,
    storage: Storage,
    ticker: str,
    run_date: date,
) -> list[PriceBar]:
    """Fetch 1-year daily prices from Yahoo via a direct HTTP call.

    We bypass YahooFinanceClient.fetch_daily_prices because that client is
    hard-coded to use range=6mo.  We reuse the same URL template and parsing
    logic inline to avoid modifying existing code.
    """
    from urllib.parse import quote
    from datetime import timezone as _tz

    encoded_ticker = quote(ticker, safe="")
    base_url = YahooFinanceClient.base_url.format(ticker=encoded_ticker)

    try:
        response = http_client.get(base_url, params=YAHOO_1Y_PARAMS)
    except Exception as exc:
        log.debug("Yahoo 1y fetch failed for %s: %s", ticker, exc)
        return []

    try:
        payload = response.json()
    except Exception:
        return []

    try:
        chart = payload.get("chart", {})
        if chart.get("error"):
            return []
        results = chart.get("result", [])
        if not results:
            return []
        result = results[0]
        timestamps = result.get("timestamp", [])
        indicators = result.get("indicators", {})
        quotes = indicators.get("quote", [{}])[0]
        adjclose_blocks = indicators.get("adjclose", [])
        adjcloses = adjclose_blocks[0].get("adjclose", []) if adjclose_blocks else []

        opens = quotes.get("open", [])
        highs = quotes.get("high", [])
        lows = quotes.get("low", [])
        closes = quotes.get("close", [])
        volumes = quotes.get("volume", [])

        ingested_at = datetime.now(_tz.utc)
        bars: list[PriceBar] = []
        safe_url = getattr(response, "safe_url", getattr(response, "url", None))

        for i, ts in enumerate(timestamps):
            close = closes[i] if i < len(closes) else None
            if i < len(adjcloses) and adjcloses[i] is not None:
                close = adjcloses[i]
            if close is None or close == 0:
                continue
            trading_date = datetime.fromtimestamp(ts, tz=_tz.utc).date()
            bars.append(PriceBar(
                ticker=ticker,
                trading_date=trading_date,
                open=float(opens[i]) if i < len(opens) and opens[i] is not None else 0.0,
                high=float(highs[i]) if i < len(highs) and highs[i] is not None else 0.0,
                low=float(lows[i]) if i < len(lows) and lows[i] is not None else 0.0,
                close=float(close),
                volume=float(volumes[i]) if i < len(volumes) and volumes[i] is not None else None,
                source="yahoo_chart",
                source_url=safe_url,
                ingested_at=ingested_at,
            ))
        bars.sort(key=lambda b: b.trading_date)
        return bars
    except Exception as exc:
        log.debug("Yahoo 1y parse failed for %s: %s", ticker, exc)
        return []


def process_ticker(
    seed: dict,
    cfg: dict,
    http_client: HttpClient,
    sec_client: SecClient,
    storage: Storage,
    cache_dir: Path,
    run_date: date,
    use_cache: bool,
    benchmark_bars: dict[str, list[PriceBar]],
) -> dict | None:
    """Process a single ticker through the full pipeline.

    Returns a row dict suitable for CSV output, or None if the ticker should
    be skipped.  Raises nothing — all exceptions must be caught by the caller.
    """
    ticker = seed["ticker"]
    filters = cfg["filters"]

    # --- Stage 1: exchange filter (already done upstream, but re-check) ---
    allowed = {ex.upper() for ex in cfg["listing"]["allowed_exchanges"]}
    exchange = seed.get("exchange", "").upper().strip()
    if exchange and exchange not in allowed:
        log.debug("[%s] exchange %s not allowed", ticker, exchange)
        return None

    # --- Stage 2: Yahoo price filter (price >= min, ADV20 >= min) ---
    bars_1y = fetch_1y_price_bars(http_client, storage, ticker, run_date)
    if not bars_1y:
        log.debug("[%s] no price data", ticker)
        return None

    bars_up_to = [b for b in bars_1y if b.trading_date <= run_date]
    if not bars_up_to:
        return None

    latest_bar = bars_up_to[-1]
    price = latest_bar.close

    if price < filters["min_price_usd"]:
        log.debug("[%s] price %.2f < min %.2f", ticker, price, filters["min_price_usd"])
        return None

    recent_20 = bars_up_to[-20:]
    dollar_vols = [b.close * b.volume for b in recent_20 if b.volume is not None and b.volume > 0]
    adv20 = sum(dollar_vols) / len(dollar_vols) if dollar_vols else 0.0
    if adv20 < filters["min_avg_dollar_volume_20d_usd"]:
        log.debug("[%s] ADV20 %.0f < min %.0f", ticker, adv20, filters["min_avg_dollar_volume_20d_usd"])
        return None

    # --- Stage 3: market cap filter (requires shares outstanding from SEC) ---
    # Fetch companyfacts JSON ONCE.  Derive both FundamentalSnapshot and
    # shares-outstanding from that single payload to avoid a duplicate HTTP call.
    # Rate-limit: sleep before the SEC call.
    cik = sec_client.cik_for_ticker(ticker, run_date)
    if not cik:
        log.debug("[%s] no CIK mapping, skip", ticker)
        return None

    time.sleep(SEC_RATE_LIMIT_INTERVAL)
    cf_url = sec_client.companyfacts_url.format(cik=cik)
    try:
        cf_response = http_client.get(cf_url)
        cf_raw = cf_response.json()
    except Exception as exc:
        log.debug("[%s] companyfacts HTTP failed: %s", ticker, exc)
        return None

    # Persist raw payload (same as SecClient.fetch_company_facts would do).
    storage.write_raw_json(run_date, "sec_companyfacts", ticker.lower(), cf_raw)

    ingested_at = datetime.now(timezone.utc)
    safe_url = getattr(cf_response, "safe_url", getattr(cf_response, "url", cf_url))
    snap: FundamentalSnapshot | None = _build_snapshot(ticker, cik, cf_raw, safe_url, ingested_at)

    if snap is None:
        log.debug("[%s] no companyfacts snapshot", ticker)
        return None

    shares_outstanding = _shares_from_payload(cf_raw, run_date)
    if shares_outstanding is None or shares_outstanding <= 0:
        log.debug("[%s] shares outstanding not available, skip", ticker)
        return None

    market_cap = price * shares_outstanding
    mcap_min = filters["market_cap_min_usd"]
    mcap_max = filters["market_cap_max_usd"]
    if not (mcap_min <= market_cap <= mcap_max):
        log.debug("[%s] market_cap %.0f outside [%.0f, %.0f]", ticker, market_cap, mcap_min, mcap_max)
        return None

    # --- Stage 4: derive price metrics (benchmark already fetched) ---
    # Pick benchmark: prefer small-cap IWM, fall back to IJH mid-cap
    bench_ticker = cfg["benchmarks"].get("small_cap", "IWM")
    bench_bars = benchmark_bars.get(bench_ticker, [])

    price_metrics = compute_price_metrics(bars_up_to, bench_bars, run_date)
    if not price_metrics:
        return None

    # --- Stage 5: derive fundamental metrics ---
    fund_metrics = compute_fundamental_metrics(snap, run_date)

    # --- Assemble row ---
    cik = fund_metrics.pop("cik", snap.cik or seed.get("cik", ""))
    row: dict = {
        "ticker": ticker,
        "name": seed.get("name", ""),
        "exchange": seed.get("exchange", ""),
        "cik": cik,
        "market_cap_usd": market_cap,
        **price_metrics,
        **fund_metrics,
        # Leave optional columns blank
        "country": "US",
        "security_type": "common_stock",
        "structure": "",
        "sector": "",
        "industry": "",
        "benchmark": bench_ticker,
        "flags": "",
        "runway_months": "",
        "interest_coverage": "",
        "share_count_growth_yoy": "",
        "working_capital_release_ratio": "",
        "earnings_days_ago": "",
        "earnings_quality": "",
        "earnings_price_confirmation": "",
        "post_earnings_relative_return_5d": "",
    }

    if use_cache:
        write_cache(cache_dir, ticker, row)

    return row


def _shares_from_payload(payload: dict, run_date: date) -> float | None:
    """Extract the latest CommonStockSharesOutstanding from an already-fetched
    companyfacts JSON dict.  Returns None if not available.
    """
    us_gaap = payload.get("facts", {}).get("us-gaap", {})
    fact = us_gaap.get("CommonStockSharesOutstanding")
    if not fact:
        # Some companies use dei namespace
        dei = payload.get("facts", {}).get("dei", {})
        fact = dei.get("EntityCommonStockSharesOutstanding")

    if not fact:
        return None

    units = fact.get("units", {})
    values = units.get("shares") or units.get("USD") or []
    # Filter to 10-K / 10-Q filings only and pick the most recent
    normalized = []
    for entry in values:
        form = entry.get("form", "")
        if form not in {"10-K", "10-Q", "20-F", "6-K"}:
            continue
        val = entry.get("val")
        end_str = entry.get("end")
        if val is None or not end_str:
            continue
        try:
            end_date = datetime.strptime(end_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if end_date > run_date:
            continue
        normalized.append((end_date, float(val)))

    if not normalized:
        return None

    normalized.sort(reverse=True)
    return normalized[0][1]


def _extract_shares_outstanding(
    http_client: HttpClient,
    sec_client: SecClient,
    ticker: str,
    run_date: date,
) -> float | None:
    """Extract the latest CommonStockSharesOutstanding value from SEC companyfacts.

    Returns None if not available.  Fetches the companyfacts JSON via HTTP
    and delegates to _shares_from_payload for the extraction logic.
    We use the cached CIK lookup to avoid re-fetching the ticker map.

    Note: process_ticker does NOT call this function — it fetches companyfacts
    once and calls _shares_from_payload directly on the in-memory payload.
    This function is retained for standalone use and unit testing.
    """
    cik = sec_client.cik_for_ticker(ticker, run_date)
    if not cik:
        return None

    url = sec_client.companyfacts_url.format(cik=cik)
    try:
        response = http_client.get(url)
        payload = response.json()
    except Exception as exc:
        log.debug("[%s] companyfacts HTTP failed: %s", ticker, exc)
        return None

    return _shares_from_payload(payload, run_date)


# ---------------------------------------------------------------------------
# Benchmark price fetching
# ---------------------------------------------------------------------------

def fetch_benchmark_bars(
    http_client: HttpClient,
    storage: Storage,
    run_date: date,
    tickers: list[str],
) -> dict[str, list[PriceBar]]:
    """Fetch 1-year bars for each benchmark ticker."""
    result: dict[str, list[PriceBar]] = {}
    for tkr in tickers:
        log.info("Fetching benchmark prices: %s", tkr)
        bars = fetch_1y_price_bars(http_client, storage, tkr, run_date)
        result[tkr] = bars
        log.info("  %s: %d bars", tkr, len(bars))
    return result


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    # Setup
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    run_date = date.fromisoformat(args.as_of) if args.as_of else date.today()
    cache_dir = Path(args.cache_dir)
    use_cache = not args.no_cache

    # Output path
    if args.output:
        output_path = Path(args.output)
    else:
        out_dir = _REPO_ROOT / "data" / "universe"
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / f"universe_{run_date.isoformat()}.csv"

    # Shared infrastructure
    http_client = HttpClient(user_agent=USER_AGENT)
    data_dir = _DEFAULT_DATA_DIR
    db_path = _DEFAULT_DB_PATH
    data_dir.mkdir(parents=True, exist_ok=True)
    storage = Storage(db_path=db_path, data_dir=data_dir)
    storage.init_db()

    sec_client = SecClient(http_client=http_client, storage=storage)

    # --- Load seed list ---
    if args.seed_file:
        log.info("Loading seed from file: %s", args.seed_file)
        seed_entries = load_seed_from_file(args.seed_file)
    else:
        seed_entries = load_seed_from_sec(http_client, run_date)

    # --- Stage 1: Exchange filter ---
    allowed_exchanges = {ex.upper() for ex in cfg["listing"]["allowed_exchanges"]}
    if args.seed_file:
        # Seed file has no exchange info — we can't pre-filter; include all.
        filtered = seed_entries
    else:
        filtered = [
            s for s in seed_entries
            if s["exchange"].upper().strip() in allowed_exchanges
        ]
    log.info("After exchange filter: %d / %d", len(filtered), len(seed_entries))

    # Apply --limit
    if args.limit is not None:
        filtered = filtered[: args.limit]
        log.info("Applying --limit %d: %d tickers", args.limit, len(filtered))

    # --- Fetch benchmark bars (once, shared across all tickers) ---
    bench_tickers = list(cfg["benchmarks"].values())
    benchmark_bars = fetch_benchmark_bars(http_client, storage, run_date, bench_tickers)

    # --- Per-ticker processing ---
    rows: list[dict] = []
    skip_reasons: list[str] = []
    n_cached = 0

    for i, seed in enumerate(filtered, 1):
        ticker = seed["ticker"]
        log.debug("[%d/%d] Processing %s", i, len(filtered), ticker)
        try:
            # Check cache first without going through full pipeline
            if use_cache:
                cached = read_cache(cache_dir, ticker)
                if cached is not None:
                    rows.append(cached)
                    n_cached += 1
                    log.debug("[%s] cache hit", ticker)
                    continue

            row = process_ticker(
                seed=seed,
                cfg=cfg,
                http_client=http_client,
                sec_client=sec_client,
                storage=storage,
                cache_dir=cache_dir,
                run_date=run_date,
                use_cache=use_cache,
                benchmark_bars=benchmark_bars,
            )
            if row is None:
                skip_reasons.append("filtered")
            else:
                rows.append(row)
        except Exception as exc:
            reason = type(exc).__name__
            skip_reasons.append(reason)
            log.warning("[%s] skipped due to %s: %s", ticker, reason, exc)

    # --- Write output CSV ---
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # Format numeric fields; leave None/empty as empty string
            out_row = {}
            for col in OUTPUT_COLUMNS:
                val = row.get(col)
                if val is None:
                    out_row[col] = ""
                elif isinstance(val, float):
                    out_row[col] = f"{val:.6g}"
                else:
                    out_row[col] = val
            writer.writerow(out_row)

    # --- Summary stats ---
    n_success = len(rows)
    n_skip = len(skip_reasons)
    top_reasons = Counter(skip_reasons).most_common(5)
    print(f"\n=== build_universe summary ===")
    print(f"Run date:    {run_date.isoformat()}")
    print(f"Seed count:  {len(filtered)}")
    print(f"Cache hits:  {n_cached}")
    print(f"Success:     {n_success}")
    print(f"Skipped:     {n_skip}")
    if top_reasons:
        print("Skip reasons (top 5):")
        for reason, count in top_reasons:
            print(f"  {reason}: {count}")
    print(f"Output:      {output_path}")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
