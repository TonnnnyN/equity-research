"""Storage-level and pure-function tests for analyst-targets history bookkeeping.

Complements test_analyst_targets.py (which exercises everything through the public
AnalystTargetsClient.fetch_analyst_snapshot path) with direct, no-HTTP-at-all tests
against Storage and the pure helper functions in market_sentiment.sources.analyst_targets.
"""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from market_sentiment.models import (
    AnalystConsensusSnapshotRow,
    AnalystRatingActionRow,
    PriceBar,
)
from market_sentiment.sources.analyst_targets import (
    compute_days_since_changed,
    compute_dispersion,
    compute_pct_change,
    find_snapshot_at_or_before,
    summarize_recent_actions,
)
from market_sentiment.storage import Storage


def _make_storage(tmp: str) -> Storage:
    storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
    storage.init_db()
    return storage


def _consensus_row(ticker: str, run_date: date, target_mean: float, security_close: float | None = None) -> AnalystConsensusSnapshotRow:
    return AnalystConsensusSnapshotRow(
        ticker=ticker,
        run_date=run_date,
        target_mean=target_mean,
        target_high=target_mean + 20,
        target_low=target_mean - 20,
        target_median=target_mean,
        number_of_analysts=10,
        recommendation_key="buy",
        recommendation_mean=2.0,
        security_close=security_close,
        source="yahoo_quote_summary",
        ingested_at=datetime.now(timezone.utc),
    )


def _action_row(ticker: str, firm: str, action_date: date, action: str = "up", to_grade: str = "Buy") -> AnalystRatingActionRow:
    return AnalystRatingActionRow(
        ticker=ticker,
        firm=firm,
        action_date=action_date,
        action=action,
        from_grade="Neutral",
        to_grade=to_grade,
        ingested_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Table 1: analyst_consensus_snapshots — idempotent on (ticker, run_date)
# ---------------------------------------------------------------------------

class ConsensusSnapshotStorageTests(TestCase):
    def test_upsert_is_idempotent_same_ticker_and_run_date_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)

            storage.upsert_analyst_consensus_snapshot(_consensus_row("MSFT", run_date, 150.0))
            storage.upsert_analyst_consensus_snapshot(_consensus_row("MSFT", run_date, 160.0))

            history = storage.get_analyst_consensus_history("MSFT")
            self.assertEqual(len(history), 1)
            self.assertAlmostEqual(history[0].target_mean, 160.0)

    def test_different_run_dates_accumulate_separate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            storage.upsert_analyst_consensus_snapshot(_consensus_row("MSFT", date(2026, 6, 1), 150.0))
            storage.upsert_analyst_consensus_snapshot(_consensus_row("MSFT", date(2026, 6, 2), 152.0))

            history = storage.get_analyst_consensus_history("MSFT")
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0].run_date, date(2026, 6, 1))
            self.assertEqual(history[1].run_date, date(2026, 6, 2))

    def test_different_tickers_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            run_date = date(2026, 6, 1)
            storage.upsert_analyst_consensus_snapshot(_consensus_row("MSFT", run_date, 150.0))
            storage.upsert_analyst_consensus_snapshot(_consensus_row("AAPL", run_date, 200.0))

            self.assertEqual(len(storage.get_analyst_consensus_history("MSFT")), 1)
            self.assertEqual(len(storage.get_analyst_consensus_history("AAPL")), 1)

    def test_history_ordered_ascending_by_run_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            storage.upsert_analyst_consensus_snapshot(_consensus_row("MSFT", date(2026, 6, 3), 155.0))
            storage.upsert_analyst_consensus_snapshot(_consensus_row("MSFT", date(2026, 6, 1), 150.0))
            storage.upsert_analyst_consensus_snapshot(_consensus_row("MSFT", date(2026, 6, 2), 152.0))

            history = storage.get_analyst_consensus_history("MSFT")
            self.assertEqual([row.run_date for row in history], [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)])


# ---------------------------------------------------------------------------
# Table 2: analyst_rating_actions — dedupe on (ticker, firm, action_date, to_grade)
# ---------------------------------------------------------------------------

class RatingActionStorageTests(TestCase):
    def test_upsert_dedupes_identical_action_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            action_date = date(2026, 5, 1)
            row = _action_row("MSFT", "Goldman Sachs", action_date)

            # Simulate re-fetching the same 388-record upgradeDowngradeHistory twice.
            storage.upsert_analyst_rating_actions([row])
            storage.upsert_analyst_rating_actions([row])

            actions = storage.get_analyst_rating_actions("MSFT")
            self.assertEqual(len(actions), 1)

    def test_first_write_wins_on_conflict_ignores_later_conflicting_row(self) -> None:
        """INSERT OR IGNORE semantics: a later row with the same dedupe key never overwrites."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            action_date = date(2026, 5, 1)
            first = _action_row("MSFT", "Goldman Sachs", action_date, to_grade="Buy")
            conflicting = AnalystRatingActionRow(
                ticker="MSFT",
                firm="Goldman Sachs",
                action_date=action_date,
                action="up",
                from_grade="DIFFERENT",
                to_grade="Buy",  # same dedupe key (ticker, firm, action_date, to_grade)
                ingested_at=datetime.now(timezone.utc),
            )
            storage.upsert_analyst_rating_actions([first])
            storage.upsert_analyst_rating_actions([conflicting])

            actions = storage.get_analyst_rating_actions("MSFT")
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].from_grade, "Neutral")  # first row's value retained

    def test_distinct_to_grade_same_day_same_firm_is_not_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            action_date = date(2026, 5, 1)
            storage.upsert_analyst_rating_actions(
                [
                    _action_row("MSFT", "Goldman Sachs", action_date, to_grade="Buy"),
                    _action_row("MSFT", "Goldman Sachs", action_date, to_grade="Strong Buy"),
                ]
            )
            self.assertEqual(len(storage.get_analyst_rating_actions("MSFT")), 2)

    def test_since_filter_excludes_older_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            storage.upsert_analyst_rating_actions(
                [
                    _action_row("MSFT", "Old Firm", date(2020, 1, 1)),
                    _action_row("MSFT", "Recent Firm", date(2026, 5, 1)),
                ]
            )
            recent = storage.get_analyst_rating_actions("MSFT", since=date(2025, 1, 1))
            self.assertEqual([a.firm for a in recent], ["Recent Firm"])

    def test_backfill_of_many_historical_actions_in_one_call(self) -> None:
        """First-ever fetch backfilling years of history — all distinct rows persist."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            rows = [
                _action_row("ZM", f"Firm {i}", date(2019, 4, 22) + timedelta(days=i * 20))
                for i in range(35)
            ]
            storage.upsert_analyst_rating_actions(rows)
            self.assertEqual(len(storage.get_analyst_rating_actions("ZM")), 35)


# ---------------------------------------------------------------------------
# get_price_on_or_before
# ---------------------------------------------------------------------------

class PriceOnOrBeforeTests(TestCase):
    def test_returns_none_when_no_prices_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            self.assertIsNone(storage.get_price_on_or_before("MSFT", date(2026, 6, 1)))

    def test_exact_date_match_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            storage.upsert_prices(
                [
                    PriceBar(ticker="MSFT", trading_date=date(2026, 6, 1), open=1, high=1, low=1, close=130.0, volume=1, source="test"),
                    PriceBar(ticker="MSFT", trading_date=date(2026, 6, 2), open=1, high=1, low=1, close=131.0, volume=1, source="test"),
                ]
            )
            result = storage.get_price_on_or_before("MSFT", date(2026, 6, 1))
            self.assertEqual(result, (date(2026, 6, 1), 130.0))

    def test_falls_back_to_nearest_earlier_trading_day_across_a_weekend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = _make_storage(tmp)
            friday = date(2026, 6, 5)
            storage.upsert_prices(
                [PriceBar(ticker="MSFT", trading_date=friday, open=1, high=1, low=1, close=125.0, volume=1, source="test")]
            )
            sunday = date(2026, 6, 7)
            result = storage.get_price_on_or_before("MSFT", sunday)
            self.assertEqual(result, (friday, 125.0))


# ---------------------------------------------------------------------------
# Pure derived-signal helpers
# ---------------------------------------------------------------------------

class PctChangeTests(TestCase):
    def test_positive_change(self) -> None:
        value, reason = compute_pct_change(110.0, 100.0)
        self.assertAlmostEqual(value, 0.10)
        self.assertIsNone(reason)

    def test_negative_change(self) -> None:
        value, reason = compute_pct_change(90.0, 100.0)
        self.assertAlmostEqual(value, -0.10)
        self.assertIsNone(reason)

    def test_none_current_yields_none_with_reason(self) -> None:
        value, reason = compute_pct_change(None, 100.0)
        self.assertIsNone(value)
        self.assertTrue(reason)

    def test_none_previous_yields_none_with_reason(self) -> None:
        value, reason = compute_pct_change(100.0, None)
        self.assertIsNone(value)
        self.assertTrue(reason)

    def test_zero_previous_yields_none_with_reason_not_zero_division_error(self) -> None:
        value, reason = compute_pct_change(100.0, 0.0)
        self.assertIsNone(value)
        self.assertIn("zero", reason)


class DispersionTests(TestCase):
    def test_zm_example_from_spec(self) -> None:
        """(135 - 79) / 115 = 0.4869..."""
        value, reason = compute_dispersion(135.0, 79.0, 115.0)
        self.assertAlmostEqual(value, (135.0 - 79.0) / 115.0, places=6)
        self.assertIsNone(reason)

    def test_missing_inputs_yield_none_with_reason(self) -> None:
        value, reason = compute_dispersion(None, 79.0, 115.0)
        self.assertIsNone(value)
        self.assertTrue(reason)

    def test_zero_target_mean_yields_none_with_reason(self) -> None:
        value, reason = compute_dispersion(135.0, 79.0, 0.0)
        self.assertIsNone(value)
        self.assertIn("zero", reason)


class FindSnapshotAtOrBeforeTests(TestCase):
    def test_returns_closest_row_at_or_before_cutoff(self) -> None:
        rows = [
            _consensus_row("MSFT", date(2026, 5, 1), 140.0),
            _consensus_row("MSFT", date(2026, 5, 15), 145.0),
            _consensus_row("MSFT", date(2026, 6, 1), 150.0),
        ]
        found = find_snapshot_at_or_before(rows, date(2026, 5, 20))
        self.assertEqual(found.run_date, date(2026, 5, 15))

    def test_returns_none_when_nothing_before_cutoff(self) -> None:
        rows = [_consensus_row("MSFT", date(2026, 6, 1), 150.0)]
        found = find_snapshot_at_or_before(rows, date(2026, 5, 1))
        self.assertIsNone(found)

    def test_exact_cutoff_match_is_included(self) -> None:
        rows = [_consensus_row("MSFT", date(2026, 5, 15), 145.0)]
        found = find_snapshot_at_or_before(rows, date(2026, 5, 15))
        self.assertIsNotNone(found)


class DaysSinceChangedTests(TestCase):
    def test_value_changed_at_the_immediately_prior_point(self) -> None:
        points = [
            (date(2026, 5, 1), 140.0),
            (date(2026, 5, 20), 150.0),  # changed here
            (date(2026, 6, 1), 150.0),  # current, same as prior point
        ]
        days, reason = compute_days_since_changed(points, date(2026, 6, 1))
        self.assertEqual(days, (date(2026, 6, 1) - date(2026, 5, 20)).days)
        self.assertIsNone(reason)

    def test_stable_across_all_history_anchors_at_earliest_point(self) -> None:
        points = [
            (date(2026, 5, 1), 150.0),
            (date(2026, 5, 20), 150.0),
            (date(2026, 6, 1), 150.0),
        ]
        days, reason = compute_days_since_changed(points, date(2026, 6, 1))
        self.assertEqual(days, (date(2026, 6, 1) - date(2026, 5, 1)).days)

    def test_single_point_yields_none_with_reason(self) -> None:
        days, reason = compute_days_since_changed([(date(2026, 6, 1), 150.0)], date(2026, 6, 1))
        self.assertIsNone(days)
        self.assertTrue(reason)

    def test_current_value_none_yields_none_with_reason(self) -> None:
        points = [(date(2026, 5, 1), 140.0), (date(2026, 6, 1), None)]
        days, reason = compute_days_since_changed(points, date(2026, 6, 1))
        self.assertIsNone(days)
        self.assertTrue(reason)


class SummarizeRecentActionsTests(TestCase):
    def test_counts_and_firms_within_window(self) -> None:
        run_date = date(2026, 6, 1)
        actions = [
            _action_row("MSFT", "Goldman Sachs", run_date - timedelta(days=5), action="up"),
            _action_row("MSFT", "Morgan Stanley", run_date - timedelta(days=40), action="down"),
            _action_row("MSFT", "JP Morgan", run_date - timedelta(days=100), action="init"),  # outside window
        ]
        summary = summarize_recent_actions(actions, run_date, window_days=90)
        self.assertEqual(summary["up"], 1)
        self.assertEqual(summary["down"], 1)
        self.assertEqual(summary["init"], 0)
        self.assertEqual(summary["firms"], ["Goldman Sachs", "Morgan Stanley"])

    def test_maintain_and_reiterate_actions_are_excluded_from_counts(self) -> None:
        run_date = date(2026, 6, 1)
        actions = [
            _action_row("MSFT", "Goldman Sachs", run_date - timedelta(days=5), action="main"),
            _action_row("MSFT", "Morgan Stanley", run_date - timedelta(days=5), action="reit"),
        ]
        summary = summarize_recent_actions(actions, run_date, window_days=90)
        self.assertEqual(summary, {"up": 0, "down": 0, "init": 0, "firms": []})

    def test_empty_actions_list(self) -> None:
        summary = summarize_recent_actions([], date(2026, 6, 1), window_days=90)
        self.assertEqual(summary, {"up": 0, "down": 0, "init": 0, "firms": []})
