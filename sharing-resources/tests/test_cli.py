from __future__ import annotations

from datetime import date, datetime, timezone
from unittest import TestCase

from equity_research.cli import build_parser, resolve_run_date


class CliTests(TestCase):
    def test_resolve_run_date_defaults_to_configured_timezone(self) -> None:
        now = datetime(2026, 3, 26, 16, 30, tzinfo=timezone.utc)

        self.assertEqual(resolve_run_date(None, "Asia/Hong_Kong", now=now), date(2026, 3, 27))
        self.assertEqual(resolve_run_date("2026-03-26", "Asia/Hong_Kong", now=now), date(2026, 3, 26))

    def test_review_accepts_multiple_tickers(self) -> None:
        args = build_parser().parse_args(["review", "ZM", "NVDA", "AMZN"])

        self.assertEqual(args.command, "review")
        self.assertEqual(args.tickers, ["ZM", "NVDA", "AMZN"])
        self.assertIsNone(args.benchmark)
        self.assertIsNone(args.layer)

    def test_cleanup_data_accepts_optional_date_override(self) -> None:
        args = build_parser().parse_args(["cleanup-data", "--date", "2026-03-26"])

        self.assertEqual(args.command, "cleanup-data")
        self.assertEqual(args.run_date, "2026-03-26")

    def test_preflight_parses_without_extra_args(self) -> None:
        args = build_parser().parse_args(["preflight"])

        self.assertEqual(args.command, "preflight")
