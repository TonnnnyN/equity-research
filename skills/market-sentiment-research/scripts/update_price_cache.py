#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import tomllib


def _resolve_root() -> Path:
    env = os.environ.get("MARKET_SENTIMENT_TARGET_POOL")
    if env:
        return Path(env).expanduser().resolve()
    home_default = Path.home() / "market_sentiment_target_pool"
    if home_default.exists():
        return home_default
    raise SystemExit(
        "Cannot find target-pool directory. Set MARKET_SENTIMENT_TARGET_POOL "
        "or create ~/market_sentiment_target_pool with a targets.toml inside."
    )


ROOT_DIR = _resolve_root()
TARGETS_PATH = ROOT_DIR / "targets.toml"
DAILY_DIR = ROOT_DIR / "prices" / "daily"
CURRENT_DIR = ROOT_DIR / "prices" / "current"
MANIFEST_PATH = ROOT_DIR / "prices" / "manifest.json"

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
USER_AGENT = "Mozilla/5.0 (compatible; market-sentiment-price-cache/1.0)"


@dataclass(slots=True)
class Target:
    ticker: str
    name: str
    layer: str
    benchmark: str


def main() -> None:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_DIR.mkdir(parents=True, exist_ok=True)

    targets = load_targets(TARGETS_PATH)
    manifest: dict[str, Any] = {
        "updated_at": now_utc().isoformat(),
        "source": "yahoo_chart",
        "targets_path": str(TARGETS_PATH),
        "target_count": len(targets),
        "tickers": {},
    }

    for target in targets:
        print(f"Updating {target.ticker}...")
        daily_info = refresh_daily_cache(target)
        current_info = refresh_current_snapshot(target)
        manifest["tickers"][target.ticker] = {
            "name": target.name,
            "layer": target.layer,
            "benchmark": target.benchmark,
            "daily": daily_info,
            "current": current_info,
        }
        time.sleep(0.2)

    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote manifest: {MANIFEST_PATH}")


def load_targets(path: Path) -> list[Target]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    targets: list[Target] = []
    seen: set[str] = set()
    for layer_block in raw.get("layers", []):
        layer = str(layer_block.get("layer"))
        benchmark = str(layer_block.get("benchmark"))
        for member in layer_block.get("members", []):
            ticker = str(member["ticker"]).upper()
            if ticker in seen:
                continue
            seen.add(ticker)
            targets.append(Target(ticker=ticker, name=str(member["name"]), layer=layer, benchmark=benchmark))
    for reference in raw.get("references", []):
        ticker = str(reference["ticker"]).upper()
        if ticker in seen:
            continue
        seen.add(ticker)
        targets.append(
            Target(
                ticker=ticker,
                name=str(reference["name"]),
                layer=str(reference.get("role") or "reference"),
                benchmark="",
            )
        )
    return targets


def refresh_daily_cache(target: Target) -> dict[str, Any]:
    path = DAILY_DIR / f"{target.ticker}.csv"
    local_rows = read_daily_rows(path)
    local_latest = latest_daily_date(local_rows)

    remote_latest = latest_remote_daily_date(target.ticker)
    if local_latest is None or (remote_latest is not None and remote_latest > local_latest):
        if local_latest is None:
            remote_rows = fetch_daily_rows(target.ticker, range_="10y")
        else:
            local_latest_date = date.fromisoformat(local_latest)
            period1 = datetime.combine(local_latest_date, datetime.min.time(), tzinfo=UTC) - timedelta(days=7)
            remote_rows = fetch_daily_rows(
                target.ticker,
                period1=int(period1.timestamp()),
                period2=int(now_utc().timestamp()) + 86400,
            )
        merged_rows = merge_daily_rows(local_rows, remote_rows)
        write_daily_rows(path, merged_rows)
        local_rows = merged_rows

    latest_date = latest_daily_date(local_rows)
    return {
        "path": str(path),
        "row_count": len(local_rows),
        "latest_trading_date": latest_date,
        "remote_latest_trading_date": remote_latest,
        "source": "yahoo_chart_1d",
    }


def refresh_current_snapshot(target: Target) -> dict[str, Any]:
    path = CURRENT_DIR / f"{target.ticker}.json"
    snapshot = fetch_current_snapshot(target.ticker)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "path": str(path),
        "fetched_at": snapshot.get("fetched_at"),
        "market_state_guess": snapshot.get("market_state_guess"),
        "regular_market_price": snapshot.get("regular_market_price"),
        "previous_close": snapshot.get("previous_close"),
        "latest_observation_time": snapshot.get("latest_observation_time"),
        "latest_observation_price": snapshot.get("latest_observation_price"),
        "source": "yahoo_chart_1m",
    }


def read_daily_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_daily_rows(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "date",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
        "currency",
        "exchange_name",
        "timezone",
        "fetched_at",
        "source",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def latest_daily_date(rows: list[dict[str, str]]) -> str | None:
    if not rows:
        return None
    return rows[-1]["date"]


def latest_remote_daily_date(ticker: str) -> str | None:
    rows = fetch_daily_rows(ticker, range_="3mo")
    if not rows:
        return None
    return rows[-1]["date"]


def fetch_daily_rows(
    ticker: str,
    *,
    range_: str | None = None,
    period1: int | None = None,
    period2: int | None = None,
) -> list[dict[str, str]]:
    payload = fetch_chart_payload(
        ticker,
        interval="1d",
        range_=range_,
        include_prepost=False,
        period1=period1,
        period2=period2,
    )
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adjclose = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    timestamps = result.get("timestamp") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    rows: list[dict[str, str]] = []
    fetched_at = now_utc().isoformat()
    for index, ts in enumerate(timestamps):
        if index >= len(closes):
            continue
        close = closes[index]
        if close in (None, 0):
            continue
        dt = datetime.fromtimestamp(ts, UTC).date().isoformat()
        rows.append(
            {
                "date": dt,
                "open": format_float(opens[index] if index < len(opens) else None),
                "high": format_float(highs[index] if index < len(highs) else None),
                "low": format_float(lows[index] if index < len(lows) else None),
                "close": format_float(close),
                "adj_close": format_float(adjclose[index] if index < len(adjclose) else close),
                "volume": format_int(volumes[index] if index < len(volumes) else None),
                "currency": str(meta.get("currency") or ""),
                "exchange_name": str(meta.get("exchangeName") or ""),
                "timezone": str(meta.get("exchangeTimezoneName") or ""),
                "fetched_at": fetched_at,
                "source": "yahoo_chart_1d",
            }
        )
    return rows


def fetch_current_snapshot(ticker: str) -> dict[str, Any]:
    payload = fetch_chart_payload(
        ticker,
        interval="1m",
        range_="1d",
        include_prepost=True,
    )
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    timestamps = result.get("timestamp") or []

    latest_price = None
    latest_ts = None
    for ts, close in zip(reversed(timestamps), reversed(closes)):
        if close is None:
            continue
        latest_ts = ts
        latest_price = close
        break

    fetched_at = now_utc()
    previous_close = meta.get("previousClose") or meta.get("chartPreviousClose")
    regular_market_price = meta.get("regularMarketPrice")
    market_state = infer_market_state(meta.get("currentTradingPeriod"), fetched_at)
    change = None
    change_pct = None
    if latest_price is not None and previous_close not in (None, 0):
        change = round(float(latest_price) - float(previous_close), 4)
        change_pct = round(change / float(previous_close), 6)

    return {
        "ticker": ticker,
        "fetched_at": fetched_at.isoformat(),
        "currency": meta.get("currency"),
        "exchange_name": meta.get("exchangeName"),
        "exchange_timezone_name": meta.get("exchangeTimezoneName"),
        "market_state_guess": market_state,
        "regular_market_price": regular_market_price,
        "previous_close": previous_close,
        "chart_previous_close": meta.get("chartPreviousClose"),
        "latest_observation_time": datetime.fromtimestamp(latest_ts, UTC).isoformat() if latest_ts else None,
        "latest_observation_price": latest_price,
        "change_from_previous_close": change,
        "change_percent_from_previous_close": change_pct,
        "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
        "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
        "current_trading_period": meta.get("currentTradingPeriod"),
        "source": "yahoo_chart_1m",
    }


def fetch_chart_payload(
    ticker: str,
    *,
    interval: str,
    include_prepost: bool,
    range_: str | None = None,
    period1: int | None = None,
    period2: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "interval": interval,
        "includePrePost": "true" if include_prepost else "false",
        "events": "div,splits",
    }
    if range_ is not None:
        params["range"] = range_
    else:
        params["period1"] = period1 or 0
        params["period2"] = period2 or int(now_utc().timestamp())

    url = YAHOO_CHART_URL.format(ticker=ticker)
    full_url = f"{url}?{urlencode(params)}"
    request = Request(full_url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    error = payload.get("chart", {}).get("error")
    if error:
        raise RuntimeError(f"Yahoo chart error for {ticker}: {error}")
    return payload


def merge_daily_rows(local_rows: list[dict[str, str]], remote_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {row["date"]: row for row in local_rows}
    for row in remote_rows:
        merged[row["date"]] = row
    return [merged[date_key] for date_key in sorted(merged)]


def infer_market_state(current_trading_period: dict[str, Any] | None, fetched_at: datetime) -> str:
    if not current_trading_period:
        return "unknown"
    now_ts = int(fetched_at.timestamp())
    for state in ("pre", "regular", "post"):
        period = current_trading_period.get(state) or {}
        start = period.get("start")
        end = period.get("end")
        if isinstance(start, int) and isinstance(end, int) and start <= now_ts < end:
            return state
    return "closed"


def now_utc() -> datetime:
    return datetime.now(UTC)


def format_float(value: Any) -> str:
    if value is None:
        return ""
    return f"{float(value):.6f}"


def format_int(value: Any) -> str:
    if value is None:
        return ""
    return str(int(value))


if __name__ == "__main__":
    main()
