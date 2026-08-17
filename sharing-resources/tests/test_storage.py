from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from market_sentiment.models import PriceBar
from market_sentiment.storage import Storage


class StorageTests(TestCase):
    def test_save_review_packets_clears_stale_files_and_delivery_prefers_manual_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()
            run_date = date(2026, 3, 26)

            storage.save_review_packets(run_date, {"AAA": {"security": {"ticker": "AAA"}}})
            storage.save_review_packets(run_date, {"BBB": {"security": {"ticker": "BBB"}}})
            packet_dir = data_dir / "reports" / run_date.isoformat() / "review_packets"

            self.assertFalse((packet_dir / "AAA.json").exists())
            self.assertTrue((packet_dir / "BBB.json").exists())

            storage.save_manual_agent_report(run_date, "# detailed report")

            self.assertEqual(storage.load_delivery_report_markdown(run_date), "# detailed report")

    def test_cleanup_retention_removes_old_artifacts_but_keeps_reference_day_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()
            reference_day = date(2026, 3, 26)
            old_day = reference_day - timedelta(days=120)

            old_report_dir = data_dir / "reports" / old_day.isoformat()
            old_report_dir.mkdir(parents=True)
            (old_report_dir / "report.md").write_text("old", encoding="utf-8")
            current_report_dir = data_dir / "reports" / reference_day.isoformat()
            current_report_dir.mkdir(parents=True)
            (current_report_dir / "report.md").write_text("current", encoding="utf-8")

            old_raw_dir = data_dir / "raw" / old_day.isoformat() / "fred"
            old_raw_dir.mkdir(parents=True)
            (old_raw_dir / "dgs10.json").write_text("{}", encoding="utf-8")
            old_social_raw_dir = data_dir / "raw" / old_day.isoformat() / "reddit"
            old_social_raw_dir.mkdir(parents=True)
            (old_social_raw_dir / "msft.json").write_text("{}", encoding="utf-8")

            with sqlite3.connect(storage.db_path) as conn:
                conn.execute(
                    "INSERT INTO runs (run_date, generated_at, triggered_count) VALUES (?, ?, ?)",
                    (old_day.isoformat(), datetime(2025, 11, 26, 12, 0, tzinfo=timezone.utc).isoformat(), 1),
                )
                conn.execute(
                    "INSERT INTO runs (run_date, generated_at, triggered_count) VALUES (?, ?, ?)",
                    (reference_day.isoformat(), datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc).isoformat(), 1),
                )
                conn.execute(
                    """
                    INSERT INTO source_payloads
                    (run_date, source, success, partial, message)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (old_day.isoformat(), "fred", 1, 0, "ok"),
                )
                conn.execute(
                    """
                    INSERT INTO scorecards
                    (run_date, ticker, layer, event_tag, triggered, total_score, state, veto_reason, partial_coverage, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (old_day.isoformat(), "AAA", "compute", "company_specific", 1, 80, "Add", None, 0, "{}"),
                )
                conn.execute(
                    """
                    INSERT INTO daily_prices
                    (ticker, trading_date, open, high, low, close, volume, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("AAA", old_day.isoformat(), 10.0, 11.0, 9.0, 10.5, 1000, "alpha"),
                )
                conn.execute(
                    """
                    INSERT INTO official_events
                    (ticker, event_time, form_type, title, url, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("AAA", f"{old_day.isoformat()}T00:00:00", "8-K", "old", "https://example.com", "sec"),
                )
                conn.execute(
                    """
                    INSERT INTO fundamental_snapshots
                    (ticker, period_end, filed_on, cik, revenue_latest, revenue_previous,
                     operating_cashflow_latest, operating_cashflow_previous, capex_latest,
                     cash_latest, debt_latest, source, notes_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("AAA", old_day.isoformat(), old_day.isoformat(), "1", 10.0, 9.0, 2.0, 1.0, 0.5, 5.0, 1.0, "sec_companyfacts", "[]"),
                )
                conn.execute(
                    """
                    INSERT INTO macro_observations
                    (name, observed_on, value, source)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("dgs10", old_day.isoformat(), 4.0, "fred"),
                )
                conn.commit()

            summary = storage.cleanup_retention(
                reference_date=reference_day,
                report_days=90,
                raw_payload_days=30,
                daily_price_days=90,
                official_event_days=90,
                fundamental_days=90,
                macro_days=90,
                run_metadata_days=90,
                social_raw_payload_days=45,
                social_post_days=90,
                social_snapshot_days=120,
            )

            self.assertIn(str(old_report_dir), summary.deleted_report_dirs)
            self.assertFalse(old_report_dir.exists())
            self.assertTrue(current_report_dir.exists())
            self.assertFalse((data_dir / "raw" / old_day.isoformat()).exists())
            self.assertFalse(old_social_raw_dir.exists())
            self.assertEqual(summary.deleted_db_rows["runs"], 1)
            self.assertEqual(summary.deleted_db_rows["daily_prices"], 1)
            with sqlite3.connect(storage.db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs WHERE run_date = ?", (reference_day.isoformat(),)).fetchone()[0], 1)

    def test_read_cached_prices_returns_recent_bars_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()

            # Upsert 3 PriceBars for ticker "TESTX" with trading_dates spanning 5 days (recent)
            base_date = date.today() - timedelta(days=5)  # 5 days ago
            bars = [
                PriceBar(
                    ticker="TESTX",
                    trading_date=base_date,
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    volume=1000,
                    source="test",
                ),
                PriceBar(
                    ticker="TESTX",
                    trading_date=base_date + timedelta(days=1),
                    open=100.5,
                    high=102.0,
                    low=100.0,
                    close=101.5,
                    volume=1100,
                    source="test",
                ),
                PriceBar(
                    ticker="TESTX",
                    trading_date=base_date + timedelta(days=5),
                    open=101.5,
                    high=103.0,
                    low=101.0,
                    close=102.5,
                    volume=1200,
                    source="test",
                ),
            ]
            storage.upsert_prices(bars)

            # Call read_cached_prices
            result = storage.read_cached_prices("TESTX", days_back=30)

            # Assert: returned list length 3, sorted by trading_date ASC, each is a PriceBar instance with correct close
            self.assertEqual(len(result), 3)
            self.assertEqual(result[0].trading_date, base_date)
            self.assertEqual(result[1].trading_date, base_date + timedelta(days=1))
            self.assertEqual(result[2].trading_date, base_date + timedelta(days=5))
            self.assertEqual(result[0].close, 100.5)
            self.assertEqual(result[1].close, 101.5)
            self.assertEqual(result[2].close, 102.5)
            for bar in result:
                self.assertIsInstance(bar, PriceBar)

    def test_read_cached_prices_reference_date_anchors_cutoff_not_wallclock_today(self) -> None:
        """A caller computing a window relative to a specific run_date (a backtest, a
        delayed run, a fixed-date test) must get a cutoff anchored to that run_date —
        not silently to wall-clock "today", which would wrongly exclude bars that are
        within `days_back` of run_date but not within `days_back` of today."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()

            run_date = date(2020, 6, 15)  # deliberately far from wall-clock "today"
            bar_date = run_date - timedelta(days=25)  # within 30 days of run_date...
            storage.upsert_prices(
                [
                    PriceBar(
                        ticker="OLDX",
                        trading_date=bar_date,
                        open=1.0,
                        high=1.0,
                        low=1.0,
                        close=1.0,
                        volume=1,
                        source="test",
                    )
                ]
            )

            # ...but obviously nowhere near 30 days of the real "today".
            without_reference = storage.read_cached_prices("OLDX", days_back=30)
            self.assertEqual(without_reference, [])

            with_reference = storage.read_cached_prices("OLDX", days_back=30, reference_date=run_date)
            self.assertEqual(len(with_reference), 1)
            self.assertEqual(with_reference[0].trading_date, bar_date)

    def test_get_earliest_and_latest_cached_price_date_return_none_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()

            self.assertIsNone(storage.get_earliest_cached_price_date("NOPE"))
            self.assertIsNone(storage.get_latest_cached_price_date("NOPE"))

    def test_get_earliest_and_latest_cached_price_date_return_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()

            base = date(2020, 1, 1)
            bars = [
                PriceBar(
                    ticker="AAPL",
                    trading_date=base + timedelta(days=offset),
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    volume=1000,
                    source="test",
                )
                for offset in (0, 10, 500, 1800)
            ]
            storage.upsert_prices(bars)

            self.assertEqual(storage.get_earliest_cached_price_date("AAPL"), base)
            self.assertEqual(storage.get_latest_cached_price_date("AAPL"), base + timedelta(days=1800))
            # A different ticker with no rows must not see AAPL's bounds.
            self.assertIsNone(storage.get_earliest_cached_price_date("MSFT"))

    def test_upsert_and_read_active_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()

            invalidate_conds = [{"metric": "price", "comparator": "<", "threshold": 95.0}]
            rerate_conds = [{"metric": "volume", "comparator": ">", "threshold": 5000000}]

            storage.upsert_active_decision(
                ticker="AAPL",
                decision_date="2026-05-16",
                state="WATCH",
                reference_close=150.0,
                invalidate_conditions=invalidate_conds,
                rerate_conditions=rerate_conds,
                status="active",
                status_reason=None,
                last_checked_date=None,
            )

            decisions = storage.read_active_decisions()
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["ticker"], "AAPL")
            self.assertEqual(decisions[0]["decision_date"], "2026-05-16")
            self.assertEqual(decisions[0]["state"], "WATCH")
            self.assertEqual(decisions[0]["reference_close"], 150.0)
            self.assertEqual(decisions[0]["invalidate_conditions"], invalidate_conds)
            self.assertEqual(decisions[0]["rerate_conditions"], rerate_conds)
            self.assertEqual(decisions[0]["status"], "active")

    def test_read_active_decisions_excludes_non_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()

            storage.upsert_active_decision(
                ticker="AAPL",
                decision_date="2026-05-16",
                state="WATCH",
                reference_close=150.0,
                invalidate_conditions=[],
                rerate_conditions=[],
                status="active",
            )

            storage.upsert_active_decision(
                ticker="MSFT",
                decision_date="2026-05-16",
                state="ADD",
                reference_close=320.0,
                invalidate_conditions=[],
                rerate_conditions=[],
                status="invalidated",
                status_reason="Price fell below threshold",
            )

            decisions = storage.read_active_decisions()
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["ticker"], "AAPL")

    def test_update_decision_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()

            storage.upsert_active_decision(
                ticker="AAPL",
                decision_date="2026-05-16",
                state="WATCH",
                reference_close=150.0,
                invalidate_conditions=[],
                rerate_conditions=[],
                status="active",
            )

            storage.update_decision_status(
                ticker="AAPL",
                decision_date="2026-05-16",
                status="invalidated",
                status_reason="Price fell below support",
                last_checked_date="2026-05-17",
            )

            decisions = storage.read_active_decisions()
            self.assertEqual(len(decisions), 0)

            all_decisions = storage.read_all_decisions_for_ticker("AAPL")
            self.assertEqual(len(all_decisions), 1)
            self.assertEqual(all_decisions[0]["status"], "invalidated")
            self.assertEqual(all_decisions[0]["status_reason"], "Price fell below support")
            self.assertEqual(all_decisions[0]["last_checked_date"], "2026-05-17")

    def test_upsert_active_decision_replaces_on_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()

            storage.upsert_active_decision(
                ticker="AAPL",
                decision_date="2026-05-16",
                state="WATCH",
                reference_close=150.0,
                invalidate_conditions=[],
                rerate_conditions=[],
                status="active",
            )

            storage.upsert_active_decision(
                ticker="AAPL",
                decision_date="2026-05-16",
                state="ADD",
                reference_close=155.0,
                invalidate_conditions=[{"metric": "rsi", "comparator": "<", "threshold": 30}],
                rerate_conditions=[],
                status="active",
            )

            with sqlite3.connect(storage.db_path) as conn:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM active_decisions WHERE ticker = ? AND decision_date = ?",
                    ("AAPL", "2026-05-16"),
                ).fetchone()
                self.assertEqual(rows[0], 1)

            decisions = storage.read_active_decisions()
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["state"], "ADD")
            self.assertEqual(decisions[0]["reference_close"], 155.0)
            self.assertEqual(len(decisions[0]["invalidate_conditions"]), 1)

    def test_condition_blobs_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()

            complex_invalidate = [
                {"metric": "price", "comparator": "<", "threshold": 95.0, "duration_days": 3},
                {"metric": "volume", "comparator": "<", "threshold": 1000000, "consecutive": True},
            ]
            complex_rerate = [
                {
                    "metric": "earnings_surprise",
                    "comparator": ">",
                    "threshold": 10,
                    "lookback_periods": 2,
                    "conditions": ["guidance_raised", "beat_expectations"],
                },
            ]

            storage.upsert_active_decision(
                ticker="GOOG",
                decision_date="2026-05-16",
                state="STARTER",
                reference_close=175.0,
                invalidate_conditions=complex_invalidate,
                rerate_conditions=complex_rerate,
            )

            all_decisions = storage.read_all_decisions_for_ticker("GOOG")
            self.assertEqual(len(all_decisions), 1)
            decision = all_decisions[0]

            self.assertEqual(decision["invalidate_conditions"], complex_invalidate)
            self.assertEqual(decision["rerate_conditions"], complex_rerate)
            self.assertEqual(decision["state"], "STARTER")
