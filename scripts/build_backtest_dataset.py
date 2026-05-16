#!/usr/bin/env python
"""CLI script to build a complete backtest dataset from config.

Fetches ~2 years of historical market data (US equities only),
SEC events, and company facts. Writes self-contained JSON files.
"""

from __future__ import annotations

import argparse
import sys
import logging
from pathlib import Path

# Add package source root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sharing-resources" / "src"))

from market_sentiment.backtest.data_loader import build_dataset


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a backtest dataset with prices, SEC data, and metadata."
    )
    parser.add_argument(
        "--config",
        default="config/watchlist.toml",
        help="Path to TOML config with securities and benchmarks (default: config/watchlist.toml)",
    )
    parser.add_argument(
        "--output-dir",
        default="data/backtest/dataset",
        help="Directory to write JSON dataset files (default: data/backtest/dataset)",
    )

    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        manifest = build_dataset(
            output_dir=args.output_dir,
            config_path=args.config,
        )

        # Print summary
        print(f"Dataset built successfully to: {args.output_dir}")
        print(f"Generated at: {manifest['generated_at']}")
        print(f"Securities: {len(manifest['tickers'])}")
        print(f"Benchmarks: {len(manifest['benchmarks'])}")
        print(f"Price bars: {sum(manifest['price_bar_counts'].values())}")

        failures = manifest.get("failures", {})
        if failures.get("prices"):
            print(f"Price failures: {len(failures['prices'])}")
        if failures.get("sec_events"):
            print(f"SEC event failures: {len(failures['sec_events'])}")
        if failures.get("companyfacts"):
            print(f"Company facts failures: {len(failures['companyfacts'])}")

    except Exception as e:
        print(f"Error building dataset: {e}", file=sys.stderr)
        logging.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
