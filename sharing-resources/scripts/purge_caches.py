#!/usr/bin/env python3
"""
缓存清理脚本 - 删除过期的社交帖子和 SEC 财报缓存。
可独立运行或由 cron 定期调用。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from market_sentiment.storage import Storage
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from market_sentiment.storage import Storage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Purge old social post and filing summary caches from SQLite."
    )
    parser.add_argument(
        "--social-days",
        type=int,
        default=14,
        help="Delete social posts older than N days (default: 14)",
    )
    parser.add_argument(
        "--filing-days",
        type=int,
        default=0,
        help="Delete filing summaries older than N days (default: 0, meaning no deletion)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to SQLite database (default: data/state/market_sentiment.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print what would be deleted without making changes",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    db_path = args.db_path or Path("data/state/market_sentiment.db")
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path

    if not db_path.exists():
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        return 1

    storage = Storage(db_path=db_path, data_dir=db_path.parent.parent / "raw")

    now = datetime.now(timezone.utc)
    social_cutoff = now - timedelta(days=max(0, args.social_days))
    filing_cutoff = now - timedelta(days=max(0, args.filing_days))

    try:
        social_deleted = 0
        filing_deleted = 0

        if not args.dry_run:
            storage.init_db()
            social_deleted = storage.purge_old_social_posts(social_cutoff)
            if args.filing_days > 0:
                filing_deleted = storage.purge_old_filing_summaries(filing_cutoff)

        if args.social_days > 0:
            action = "Would purge" if args.dry_run else "Purged"
            cutoff_date = social_cutoff.date().isoformat()
            print(f"[social] {action} {social_deleted} posts older than {cutoff_date}")

        if args.filing_days > 0:
            action = "Would purge" if args.dry_run else "Purged"
            cutoff_date = filing_cutoff.date().isoformat()
            print(f"[filing] {action} {filing_deleted} filings older than {cutoff_date}")
        else:
            print("[filing] skipped (filing-days=0)")

        return 0
    except Exception as exc:
        print(f"Error during purge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
