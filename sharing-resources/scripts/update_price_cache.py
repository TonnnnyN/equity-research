#!/usr/bin/env python3
"""
价格缓存预热/续新脚本 - 为 watchlist 中的每个标的和基准补齐深度价格历史。

Deepens and tops up the SQLite `daily_prices` cache for every security and benchmark
in the configured watchlist, WITHOUT running the full daily pipeline (no SEC/social/
options/macro lanes) — useful for warming a fresh database before the first real run,
or for a lightweight standalone cron job that keeps price history current on its own
schedule.

Safe to run repeatedly and idempotent: a ticker whose cache already covers
`config.PRICE_HISTORY_TARGET_DAYS` only pulls the incremental gap since its latest
cached bar; a ticker with no cache yet (or a shallow/legacy cache predating this
depth policy) gets a one-time deep backfill. See `DailyPipeline.get_price_history`
for the underlying decision logic — this script is a thin driver over it.

Usage:
    python3 scripts/update_price_cache.py [--date YYYY-MM-DD] [--config PATH]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

try:
    from equity_research.config import PRICE_HISTORY_TARGET_DAYS, load_config
    from equity_research.pipeline import DailyPipeline
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from equity_research.config import PRICE_HISTORY_TARGET_DAYS, load_config
    from equity_research.pipeline import DailyPipeline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deep-backfill and/or incrementally top up the daily price cache "
        "for every security and benchmark in the watchlist."
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Optional path to the project TOML config (default: config/watchlist.toml).",
    )
    parser.add_argument(
        "--date",
        dest="run_date",
        default=None,
        help="Reference date in YYYY-MM-DD format. Defaults to today.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pipeline = DailyPipeline(config=load_config(args.config_path) if args.config_path else None)
    pipeline.init_db()

    run_date = (
        datetime.strptime(args.run_date, "%Y-%m-%d").date() if args.run_date else date.today()
    )

    tickers = sorted(
        {security.ticker for security in pipeline.config.securities}
        | {benchmark.ticker for benchmark in pipeline.config.benchmarks.values()}
    )

    if not tickers:
        print("No securities or benchmarks configured; nothing to do.")
        return 0

    print(f"Updating price cache for {len(tickers)} ticker(s) as of {run_date.isoformat()} "
          f"(target depth: {PRICE_HISTORY_TARGET_DAYS} days / ~{PRICE_HISTORY_TARGET_DAYS / 365:.1f}y)")

    exit_code = 0
    for ticker in tickers:
        was_deep, _ = pipeline._price_fetch_plan(ticker, run_date)
        try:
            prices, status, _all_statuses = pipeline.get_price_history(ticker, run_date)
        except Exception as exc:
            print(f"  {ticker}: FAILED — {exc}", file=sys.stderr)
            exit_code = 1
            continue

        mode = "deep backfill" if was_deep else "incremental"
        if not prices:
            print(f"  {ticker}: 0 bars cached [{mode}] — winning source: {status.source} ({status.message})")
            continue

        earliest = prices[0].trading_date
        latest = prices[-1].trading_date
        span_days = (latest - earliest).days
        print(
            f"  {ticker}: {len(prices)} bars cached, {earliest.isoformat()} -> {latest.isoformat()} "
            f"(~{span_days} days deep) [{mode}] — winning source: {status.source}"
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
