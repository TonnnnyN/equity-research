"""Data loader that fetches ~2 years of historical market data for US equities.

Builds a self-contained JSON dataset with prices, SEC events, and company facts.
This dataset feeds the backtest pipeline (status_engine -> simulator -> runner).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

from market_sentiment.config import load_config
from market_sentiment.http import HttpClient
from market_sentiment.storage import Storage
from market_sentiment.sources.sec import SecClient

logger = logging.getLogger(__name__)


def build_dataset(
    output_dir: str | Path,
    config_path: str = "config/watchlist.toml",
    user_agent: str | None = None,
) -> dict:
    """Build a complete backtest dataset with prices, SEC data, and metadata.

    Args:
        output_dir: Directory to write JSON dataset files
        config_path: Path to TOML config with securities and benchmarks
        user_agent: Custom User-Agent for HTTP requests (SEC requires descriptive string)

    Returns:
        Manifest dict with metadata and failure tracking
    """
    output_dir = Path(output_dir)

    # Load config to get tickers
    config = load_config(config_path)

    # Resolve user agent: env var > function arg > fallback
    sec_user_agent = (
        os.environ.get("SEC_USER_AGENT")
        or user_agent
        or "market-sentiment-backtest research contact@example.com"
    )

    # Set up HTTP client and SEC client
    http_client = HttpClient(user_agent=sec_user_agent)
    storage = Storage(
        Path("data/state/market_sentiment.sqlite3"),
        Path("data")
    )
    sec_client = SecClient(http_client, storage)

    # Filter to US tickers only (exclude .HK)
    us_securities = [s for s in config.securities if not s.ticker.endswith(".HK")]
    us_benchmarks = [b for b in config.benchmarks.values() if not b.ticker.endswith(".HK")]

    run_date = date.today()
    generated_at = datetime.now(timezone.utc)

    # Track failures
    failures = {
        "prices": [],
        "sec_events": [],
        "companyfacts": [],
    }

    # Fetch prices for all US tickers (securities + benchmarks)
    price_bar_counts = {}
    all_tickers = list({s.ticker for s in us_securities} | {b.ticker for b in us_benchmarks})

    for ticker in sorted(all_tickers):
        try:
            bars = _fetch_prices(http_client, ticker)
            if bars:
                _write_prices_json(output_dir, ticker, bars)
                price_bar_counts[ticker] = len(bars)
            else:
                failures["prices"].append(f"{ticker}: no data returned")
        except Exception as e:
            failures["prices"].append(f"{ticker}: {str(e)}")
            logger.warning(f"Failed to fetch prices for {ticker}: {e}")

    # Fetch SEC data for securities only (not benchmarks)
    for security in us_securities:
        ticker = security.ticker

        # Fetch recent events
        try:
            payload = sec_client.fetch_recent_events(ticker, run_date)
            if payload.data:
                cik = sec_client.cik_for_ticker(ticker, run_date)
                _write_sec_events_json(output_dir, ticker, cik, payload.data)
            else:
                failures["sec_events"].append(f"{ticker}: no events")
        except Exception as e:
            failures["sec_events"].append(f"{ticker}: {str(e)}")
            logger.warning(f"Failed to fetch SEC events for {ticker}: {e}")

        # Fetch company facts (raw companyfacts JSON)
        try:
            cik = sec_client.cik_for_ticker(ticker, run_date)
            if cik:
                companyfacts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
                response = http_client.get(companyfacts_url)
                companyfacts_payload = response.json()
                _write_companyfacts_json(output_dir, ticker, companyfacts_payload)
            else:
                failures["companyfacts"].append(f"{ticker}: no CIK mapping")
        except Exception as e:
            failures["companyfacts"].append(f"{ticker}: {str(e)}")
            logger.warning(f"Failed to fetch company facts for {ticker}: {e}")

    # Build and write manifest
    manifest = {
        "generated_at": generated_at.isoformat(),
        "tickers": [s.ticker for s in us_securities],
        "benchmarks": [b.ticker for b in us_benchmarks],
        "price_bar_counts": price_bar_counts,
        "failures": failures,
    }

    manifest_path = output_dir / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return manifest


def _fetch_prices(http_client: HttpClient, ticker: str) -> list[dict]:
    """Fetch ~2 years of price data from Yahoo Finance.

    Returns list of bars sorted ascending by date, with adjusted OHLCV.
    """
    # URL encode ticker (though US tickers typically have no special chars)
    encoded_ticker = quote(ticker, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_ticker}"

    params = {
        "interval": "1d",
        "range": "2y",
        "events": "div,splits",
    }

    response = http_client.get(url, params=params)
    payload = response.json()

    # Navigate the nested response structure
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError) as e:
        logger.warning(f"Invalid Yahoo Finance response structure for {ticker}: {e}")
        return []

    timestamps = result.get("timestamp", [])
    quote_data = result.get("indicators", {}).get("quote", [{}])[0]
    adjclose_data = result.get("indicators", {}).get("adjclose", [{}])[0]

    opens = quote_data.get("open", [])
    highs = quote_data.get("high", [])
    lows = quote_data.get("low", [])
    closes = quote_data.get("close", [])
    volumes = quote_data.get("volume", [])
    adjcloses = adjclose_data.get("adjclose", [])

    bars = []
    for i, ts in enumerate(timestamps):
        # Extract raw values
        close = closes[i] if i < len(closes) else None
        if close is None or close == 0:
            continue  # Skip bars with no close

        adjclose = adjcloses[i] if i < len(adjcloses) else None

        # Compute adjustment factor
        if adjclose and adjclose != 0:
            factor = adjclose / close
        else:
            adjclose = close
            factor = 1.0

        # Extract and adjust OHLCV
        open_raw = opens[i] if i < len(opens) else None
        high_raw = highs[i] if i < len(highs) else None
        low_raw = lows[i] if i < len(lows) else None
        volume_raw = volumes[i] if i < len(volumes) else None

        open_adj = round(open_raw * factor, 6) if open_raw else None
        high_adj = round(high_raw * factor, 6) if high_raw else None
        low_adj = round(low_raw * factor, 6) if low_raw else None
        close_adj = round(adjclose, 6)
        volume_adj = (
            round(volume_raw / factor, 6)
            if volume_raw and factor != 0
            else volume_raw
        )

        # Convert timestamp to date
        bar_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()

        bars.append({
            "date": bar_date.isoformat(),
            "open": open_adj,
            "high": high_adj,
            "low": low_adj,
            "close": close_adj,
            "volume": volume_adj,
        })

    # Sort ascending by date
    bars.sort(key=lambda b: b["date"])

    return bars


def _write_prices_json(output_dir: Path, ticker: str, bars: list[dict]) -> None:
    """Write price bars to {output_dir}/prices/{TICKER}.json."""
    prices_dir = output_dir / "prices"
    prices_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "ticker": ticker,
        "source": "yahoo_chart",
        "bars": bars,
    }

    path = prices_dir / f"{ticker}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_sec_events_json(
    output_dir: Path,
    ticker: str,
    cik: str | None,
    events: list,
) -> None:
    """Write SEC events to {output_dir}/sec_events/{TICKER}.json."""
    sec_events_dir = output_dir / "sec_events"
    sec_events_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "ticker": ticker,
        "cik": cik,
        "events": [
            {
                "event_time": event.event_time.isoformat(),
                "form_type": event.form_type,
                "title": event.title,
                "url": event.url,
            }
            for event in events
        ],
    }

    path = sec_events_dir / f"{ticker}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_companyfacts_json(
    output_dir: Path,
    ticker: str,
    payload: dict,
) -> None:
    """Write raw SEC companyfacts payload to {output_dir}/companyfacts/{TICKER}.json."""
    companyfacts_dir = output_dir / "companyfacts"
    companyfacts_dir.mkdir(parents=True, exist_ok=True)

    path = companyfacts_dir / f"{ticker}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
