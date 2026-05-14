from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from market_sentiment.models import ActionState, BucketScore, DailyRunReport, EventTag, Layer, PriceBar, ScoreCard, Security, SourceStatus, TriggerResult
from market_sentiment.storage import Storage


def make_scorecard(ticker: str, run_date: date) -> ScoreCard:
    security = Security(ticker=ticker, name=ticker, layer=Layer.COMPUTE, benchmark="SOXX")
    trigger = TriggerResult(
        triggered=True,
        reasons=["ten_day_drawdown"],
        ten_day_drawdown=0.2,
        twenty_day_drawdown=0.25,
        relative_underperformance=0.1,
        new_low=False,
    )
    return ScoreCard(
        run_date=run_date,
        security=security,
        event_tag=EventTag.COMPANY_SPECIFIC,
        triggered=True,
        trigger=trigger,
        fundamentals=BucketScore("fundamentals", 10, 30),
        sentiment=BucketScore("sentiment", 6, 15),
        chain_confirmation=BucketScore("chain_confirmation", 12, 20),
        price_flow=BucketScore("price_flow", 4, 15),
        risk_red_flags=BucketScore("risk_red_flags", 8, 20),
        total_score=40,
        state=ActionState.ADD,
        partial_coverage=False,
        evidence=["8-K"],
    )


class StorageTests(TestCase):
    def test_save_report_clears_stale_scorecards_for_same_run_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            storage = Storage(data_dir / "state.db", data_dir)
            storage.init_db()
            run_date = date(2026, 3, 26)

            first_report = DailyRunReport(
                run_date=run_date,
                generated_at=datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc),
                triggered_count=2,
                scorecards=[make_scorecard("AAA", run_date), make_scorecard("BBB", run_date)],
                source_statuses=[SourceStatus(source="sec", success=True, message="ok")],
            )
            second_report = DailyRunReport(
                run_date=run_date,
                generated_at=datetime(2026, 3, 26, 13, 0, tzinfo=timezone.utc),
                triggered_count=1,
                scorecards=[make_scorecard("CCC", run_date)],
                source_statuses=[SourceStatus(source="sec", success=True, message="ok")],
            )

            storage.save_report(first_report)
            storage.save_report(second_report)

            with sqlite3.connect(storage.db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM scorecards WHERE run_date = ?", (run_date.isoformat(),)).fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT ticker FROM scorecards WHERE run_date = ?", (run_date.isoformat(),)).fetchone()[0], "CCC")

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

            storage.save_report(
                DailyRunReport(
                    run_date=run_date,
                    generated_at=datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc),
                    triggered_count=0,
                    scorecards=[],
                    source_statuses=[],
                )
            )
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
