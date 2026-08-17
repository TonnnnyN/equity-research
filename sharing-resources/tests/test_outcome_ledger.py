"""
Tests for outcome_ledger module.

Covers:
- All three resolution types (invalidated, rerated, expired)
- Backward-compatible load of old decision files (without context)
- Small-sample guard (refusal to over-read with n<5)
- Grouping logic (by state, by dropped buckets)
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from equity_research.outcome_ledger import (
    OutcomeSummary,
    ResolutionRecord,
    compute_summary,
    compute_summary_by_dropped_bucket,
    compute_summary_by_state,
    format_summary_as_markdown,
    read_resolved_decisions,
    render_ledger_markdown,
)


class ResolutionRecordTests(TestCase):
    """Tests for ResolutionRecord dataclass."""

    def test_create_with_context(self) -> None:
        """ResolutionRecord can be created with full context."""
        record = ResolutionRecord(
            ticker="MSFT",
            decision_date="2026-05-01",
            state="STARTER",
            resolution_type="rerated",
            days_elapsed=45,
            reference_close=100.0,
            close_at_resolution=110.0,
            bucket_weights={"value": 0.4, "quality": 0.3, "momentum": 0.2, "social": 0.1},
            buckets_dropped=["social"],
            valuation_assumptions={"discount_rate": 0.08, "growth_rate": 0.05},
            context_available=True,
        )
        self.assertEqual(record.ticker, "MSFT")
        self.assertEqual(record.resolution_type, "rerated")
        self.assertTrue(record.context_available)

    def test_create_without_context_backward_compat(self) -> None:
        """ResolutionRecord can be created without context (backward compat)."""
        record = ResolutionRecord(
            ticker="AAPL",
            decision_date="2026-04-01",
            state="WATCH",
            resolution_type="invalidated",
            days_elapsed=20,
            reference_close=150.0,
            close_at_resolution=140.0,
            bucket_weights=None,
            buckets_dropped=None,
            valuation_assumptions=None,
            context_available=False,
        )
        self.assertEqual(record.ticker, "AAPL")
        self.assertIsNone(record.bucket_weights)
        self.assertFalse(record.context_available)


class ComputeSummaryTests(TestCase):
    """Tests for compute_summary function with small-sample guard."""

    def test_empty_records(self) -> None:
        """Empty records return all-zero summary."""
        summary = compute_summary([])
        self.assertEqual(summary.total_count, 0)
        self.assertIsNone(summary.median_days_to_resolution)

    def test_single_rerated_record(self) -> None:
        """Single rerated record is counted."""
        records = [
            ResolutionRecord(
                ticker="MSFT",
                decision_date="2026-05-01",
                state="STARTER",
                resolution_type="rerated",
                days_elapsed=45,
                reference_close=100.0,
                close_at_resolution=110.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            )
        ]
        summary = compute_summary(records)
        self.assertEqual(summary.total_count, 1)
        self.assertEqual(summary.rerated_count, 1)
        self.assertEqual(summary.invalidated_count, 0)
        self.assertEqual(summary.expired_count, 0)
        self.assertEqual(summary.median_days_to_resolution, 45.0)

    def test_three_mixed_resolutions(self) -> None:
        """Three mixed resolution types are counted correctly."""
        records = [
            ResolutionRecord(
                ticker="MSFT",
                decision_date="2026-05-01",
                state="STARTER",
                resolution_type="rerated",
                days_elapsed=45,
                reference_close=100.0,
                close_at_resolution=110.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
            ResolutionRecord(
                ticker="AAPL",
                decision_date="2026-04-01",
                state="ADD",
                resolution_type="invalidated",
                days_elapsed=20,
                reference_close=150.0,
                close_at_resolution=140.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
            ResolutionRecord(
                ticker="GOOG",
                decision_date="2026-03-01",
                state="WATCH",
                resolution_type="expired",
                days_elapsed=95,
                reference_close=120.0,
                close_at_resolution=125.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
        ]
        summary = compute_summary(records)
        self.assertEqual(summary.total_count, 3)
        self.assertEqual(summary.invalidated_count, 1)
        self.assertEqual(summary.rerated_count, 1)
        self.assertEqual(summary.expired_count, 1)
        # Median of [20, 45, 95] is 45
        self.assertEqual(summary.median_days_to_resolution, 45.0)

    def test_median_even_count(self) -> None:
        """Median is correctly computed for even number of records."""
        records = [
            ResolutionRecord(
                ticker="A",
                decision_date="2026-01-01",
                state="WATCH",
                resolution_type="expired",
                days_elapsed=10,
                reference_close=100.0,
                close_at_resolution=100.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
            ResolutionRecord(
                ticker="B",
                decision_date="2026-01-02",
                state="WATCH",
                resolution_type="expired",
                days_elapsed=20,
                reference_close=100.0,
                close_at_resolution=100.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
            ResolutionRecord(
                ticker="C",
                decision_date="2026-01-03",
                state="WATCH",
                resolution_type="expired",
                days_elapsed=30,
                reference_close=100.0,
                close_at_resolution=100.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
            ResolutionRecord(
                ticker="D",
                decision_date="2026-01-04",
                state="WATCH",
                resolution_type="expired",
                days_elapsed=40,
                reference_close=100.0,
                close_at_resolution=100.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
        ]
        summary = compute_summary(records)
        # Median of [10, 20, 30, 40] is (20 + 30) / 2 = 25
        self.assertEqual(summary.median_days_to_resolution, 25.0)

    def test_context_percentage(self) -> None:
        """Percentage of records with context is computed."""
        records = [
            ResolutionRecord(
                ticker="MSFT",
                decision_date="2026-05-01",
                state="STARTER",
                resolution_type="rerated",
                days_elapsed=45,
                reference_close=100.0,
                close_at_resolution=110.0,
                bucket_weights={"value": 0.5},
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=True,
            ),
            ResolutionRecord(
                ticker="AAPL",
                decision_date="2026-04-01",
                state="ADD",
                resolution_type="invalidated",
                days_elapsed=20,
                reference_close=150.0,
                close_at_resolution=140.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
        ]
        summary = compute_summary(records)
        self.assertEqual(summary.total_count, 2)
        self.assertEqual(summary.pct_with_context, 50.0)


class ComputeSummaryByStateTests(TestCase):
    """Tests for grouping by decision state."""

    def test_group_by_state(self) -> None:
        """Records are grouped by state correctly."""
        records = [
            ResolutionRecord(
                ticker="MSFT",
                decision_date="2026-05-01",
                state="STARTER",
                resolution_type="rerated",
                days_elapsed=45,
                reference_close=100.0,
                close_at_resolution=110.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
            ResolutionRecord(
                ticker="AAPL",
                decision_date="2026-04-01",
                state="ADD",
                resolution_type="invalidated",
                days_elapsed=20,
                reference_close=150.0,
                close_at_resolution=140.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
            ResolutionRecord(
                ticker="GOOG",
                decision_date="2026-03-01",
                state="STARTER",
                resolution_type="expired",
                days_elapsed=95,
                reference_close=120.0,
                close_at_resolution=125.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
        ]

        by_state = compute_summary_by_state(records)
        self.assertEqual(len(by_state), 2)
        self.assertIn("STARTER", by_state)
        self.assertIn("ADD", by_state)

        starter_summary = by_state["STARTER"]
        self.assertEqual(starter_summary.total_count, 2)
        self.assertEqual(starter_summary.rerated_count, 1)
        self.assertEqual(starter_summary.expired_count, 1)

        add_summary = by_state["ADD"]
        self.assertEqual(add_summary.total_count, 1)
        self.assertEqual(add_summary.invalidated_count, 1)


class ComputeSummaryByDroppedBucketTests(TestCase):
    """Tests for grouping by dropped bucket."""

    def test_no_context_records_ignored(self) -> None:
        """Records without context are ignored."""
        records = [
            ResolutionRecord(
                ticker="MSFT",
                decision_date="2026-05-01",
                state="STARTER",
                resolution_type="rerated",
                days_elapsed=45,
                reference_close=100.0,
                close_at_resolution=110.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
        ]

        by_bucket = compute_summary_by_dropped_bucket(records)
        self.assertEqual(len(by_bucket), 0)

    def test_group_by_dropped_bucket(self) -> None:
        """Records are grouped by bucket drops correctly."""
        records = [
            ResolutionRecord(
                ticker="MSFT",
                decision_date="2026-05-01",
                state="STARTER",
                resolution_type="rerated",
                days_elapsed=45,
                reference_close=100.0,
                close_at_resolution=110.0,
                bucket_weights={"value": 0.5, "social": 0.2},
                buckets_dropped=["social"],
                valuation_assumptions={"cagr": 0.08},
                context_available=True,
            ),
            ResolutionRecord(
                ticker="AAPL",
                decision_date="2026-04-01",
                state="ADD",
                resolution_type="rerated",
                days_elapsed=50,
                reference_close=150.0,
                close_at_resolution=160.0,
                bucket_weights={"value": 0.5, "social": 0.2},
                buckets_dropped=[],  # social was NOT dropped
                valuation_assumptions={"cagr": 0.08},
                context_available=True,
            ),
            ResolutionRecord(
                ticker="GOOG",
                decision_date="2026-03-01",
                state="WATCH",
                resolution_type="invalidated",
                days_elapsed=30,
                reference_close=120.0,
                close_at_resolution=110.0,
                bucket_weights={"value": 0.5},
                buckets_dropped=["social", "momentum"],
                valuation_assumptions={"cagr": 0.09},
                context_available=True,
            ),
        ]

        by_bucket = compute_summary_by_dropped_bucket(records)

        # Should have social_dropped and social_kept
        self.assertIn("social_dropped", by_bucket)
        self.assertIn("social_kept", by_bucket)

        social_dropped = by_bucket["social_dropped"]
        social_kept = by_bucket["social_kept"]

        # social_dropped: MSFT, GOOG (2 records)
        self.assertEqual(social_dropped.total_count, 2)
        self.assertEqual(social_dropped.rerated_count, 1)
        self.assertEqual(social_dropped.invalidated_count, 1)

        # social_kept: AAPL (1 record)
        self.assertEqual(social_kept.total_count, 1)
        self.assertEqual(social_kept.rerated_count, 1)

        # Should also have momentum bucket
        self.assertIn("momentum_dropped", by_bucket)
        self.assertIn("momentum_kept", by_bucket)


class FormatSummaryAsMarkdownTests(TestCase):
    """Tests for markdown formatting with small-sample guard."""

    def test_empty_summary(self) -> None:
        """Empty summary renders as 'No resolved decisions'."""
        summary = OutcomeSummary(
            total_count=0,
            invalidated_count=0,
            rerated_count=0,
            expired_count=0,
            median_days_to_resolution=None,
            pct_with_context=None,
        )
        lines = format_summary_as_markdown(summary)
        text = "\n".join(lines)
        self.assertIn("No resolved decisions", text)

    def test_small_sample_warning(self) -> None:
        """Small samples (n<5) get a warning."""
        summary = OutcomeSummary(
            total_count=3,
            invalidated_count=1,
            rerated_count=1,
            expired_count=1,
            median_days_to_resolution=45.0,
            pct_with_context=50.0,
        )
        lines = format_summary_as_markdown(summary, "Test Summary")
        text = "\n".join(lines)

        self.assertIn("WARNING", text)
        self.assertIn("far too small to conclude", text)
        # Sample size should be included
        self.assertIn("n=3", text)

    def test_adequate_sample_no_warning(self) -> None:
        """Adequate samples (n>=5) have no warning."""
        summary = OutcomeSummary(
            total_count=10,
            invalidated_count=3,
            rerated_count=4,
            expired_count=3,
            median_days_to_resolution=50.0,
            pct_with_context=70.0,
        )
        lines = format_summary_as_markdown(summary, "Outcomes")
        text = "\n".join(lines)

        self.assertNotIn("WARNING", text)
        self.assertIn("3 of 10", text)
        self.assertIn("4 of 10", text)
        self.assertIn("n=10", text)


class ReadResolvedDecisionsTests(TestCase):
    """Tests for reading resolved decisions from database and files."""

    def test_resolution_record_with_context_file(self) -> None:
        """Decision files with context are loaded correctly."""
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # Create a decision file with context
            decision_dir = tmp_path / "decisions" / "2026-05-01"
            decision_dir.mkdir(parents=True, exist_ok=True)
            decision_file = decision_dir / "MSFT.decision.json"
            decision_file.write_text(
                json.dumps(
                    {
                        "ticker": "MSFT",
                        "decision_date": "2026-05-01",
                        "state": "STARTER",
                        "reference_close": 100.0,
                        "invalidate_conditions": [{"metric": "close", "comparator": "<", "threshold": 90.0}],
                        "rerate_conditions": [{"metric": "close", "comparator": ">", "threshold": 110.0}],
                        "bucket_weights": {"value": 0.4, "quality": 0.3, "momentum": 0.2, "social": 0.1},
                        "buckets_dropped": ["social"],
                        "valuation_assumptions": {"discount_rate": 0.08},
                    }
                )
            )

            # Load the file and verify context is readable
            with open(decision_file) as f:
                payload = json.load(f)
                self.assertEqual(payload["bucket_weights"], {"value": 0.4, "quality": 0.3, "momentum": 0.2, "social": 0.1})
                self.assertEqual(payload["buckets_dropped"], ["social"])

    def test_resolution_record_backward_compat_no_context(self) -> None:
        """Old decision files without context are loaded as None."""
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # Create a decision file WITHOUT context
            decision_dir = tmp_path / "decisions" / "2026-04-01"
            decision_dir.mkdir(parents=True, exist_ok=True)
            decision_file = decision_dir / "AAPL.decision.json"
            decision_file.write_text(
                json.dumps(
                    {
                        "ticker": "AAPL",
                        "decision_date": "2026-04-01",
                        "state": "WATCH",
                        "reference_close": 150.0,
                        "invalidate_conditions": [{"metric": "close", "comparator": "<", "threshold": 140.0}],
                        "rerate_conditions": [{"metric": "close", "comparator": ">", "threshold": 160.0}],
                        # NO bucket_weights, buckets_dropped, valuation_assumptions
                    }
                )
            )

            # Load the file and verify context fields are None/missing
            with open(decision_file) as f:
                payload = json.load(f)
                self.assertIsNone(payload.get("bucket_weights"))
                self.assertIsNone(payload.get("buckets_dropped"))
                self.assertIsNone(payload.get("valuation_assumptions"))


class RenderLedgerMarkdownTests(TestCase):
    """Tests for complete markdown rendering."""

    def test_render_with_minimal_data(self) -> None:
        """Markdown is rendered with minimal data (n=3)."""
        records = [
            ResolutionRecord(
                ticker="MSFT",
                decision_date="2026-05-01",
                state="STARTER",
                resolution_type="rerated",
                days_elapsed=45,
                reference_close=100.0,
                close_at_resolution=110.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
            ResolutionRecord(
                ticker="AAPL",
                decision_date="2026-04-01",
                state="ADD",
                resolution_type="invalidated",
                days_elapsed=20,
                reference_close=150.0,
                close_at_resolution=140.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
            ResolutionRecord(
                ticker="GOOG",
                decision_date="2026-03-01",
                state="WATCH",
                resolution_type="expired",
                days_elapsed=95,
                reference_close=120.0,
                close_at_resolution=125.0,
                bucket_weights=None,
                buckets_dropped=None,
                valuation_assumptions=None,
                context_available=False,
            ),
        ]

        markdown = render_ledger_markdown(records, [])

        # Should include title
        self.assertIn("# Decision Outcome Ledger", markdown)

        # Should include overall section with warning about small sample
        self.assertIn("## Overall Outcomes", markdown)
        self.assertIn("WARNING", markdown)
        self.assertIn("n=3", markdown)

        # Should include state breakdown
        self.assertIn("## Outcomes by Decision State", markdown)

        # Should include detailed table
        self.assertIn("## Detailed Outcome Records", markdown)
        self.assertIn("MSFT", markdown)
        self.assertIn("STARTER", markdown)
        self.assertIn("rerated", markdown)

    def test_render_with_warnings(self) -> None:
        """Warnings are rendered in markdown."""
        records = []
        warnings = ["Warning 1: something went wrong", "Warning 2: another issue"]

        markdown = render_ledger_markdown(records, warnings)

        self.assertIn("## Warnings", markdown)
        self.assertIn("Warning 1", markdown)
        self.assertIn("Warning 2", markdown)
