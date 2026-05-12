from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from market_sentiment.models import FilingSummaryCacheRow, SocialPostCacheRow
from market_sentiment.storage import Storage


class CacheStorageTests(TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name)
        self.storage = Storage(self.data_dir / "test.db", self.data_dir)
        self.storage.init_db()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_upsert_social_post_cache_is_idempotent(self) -> None:
        now = datetime.now(timezone.utc)
        post1 = SocialPostCacheRow(
            source="reddit",
            post_id="post-123",
            ticker="AAPL",
            posted_at=now,
            title="Test Post",
            sentiment="bull",
            confidence=0.85,
            one_line_summary="Apple rallying hard",
            engagement_score=120.0,
            ingested_at=now,
        )

        self.storage.upsert_social_post_cache([post1])
        rows1 = self.storage.get_social_posts_for_ticker("AAPL")
        self.assertEqual(len(rows1), 1)
        self.assertEqual(rows1[0].sentiment, "bull")

        post2 = SocialPostCacheRow(
            source="reddit",
            post_id="post-123",
            ticker="AAPL",
            posted_at=now,
            title="Test Post",
            sentiment="bear",
            confidence=0.95,
            one_line_summary="Apple rallying hard",
            engagement_score=120.0,
            ingested_at=now,
        )

        self.storage.upsert_social_post_cache([post2])
        rows2 = self.storage.get_social_posts_for_ticker("AAPL")
        self.assertEqual(len(rows2), 1)
        self.assertEqual(rows2[0].sentiment, "bull")

    def test_get_social_posts_filters_by_ticker_and_since(self) -> None:
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(days=20)
        recent_time = now - timedelta(days=5)

        posts = [
            SocialPostCacheRow(
                source="reddit",
                post_id="old-1",
                ticker="AAPL",
                posted_at=old_time,
                title="Old Post",
                sentiment="bull",
                confidence=0.8,
                one_line_summary="Old",
                engagement_score=10.0,
                ingested_at=old_time,
            ),
            SocialPostCacheRow(
                source="x",
                post_id="recent-1",
                ticker="AAPL",
                posted_at=recent_time,
                title="Recent Post",
                sentiment="bear",
                confidence=0.9,
                one_line_summary="Recent",
                engagement_score=50.0,
                ingested_at=recent_time,
            ),
            SocialPostCacheRow(
                source="reddit",
                post_id="msft-1",
                ticker="MSFT",
                posted_at=recent_time,
                title="Microsoft Post",
                sentiment="bull",
                confidence=0.75,
                one_line_summary="MSFT news",
                engagement_score=30.0,
                ingested_at=recent_time,
            ),
        ]
        self.storage.upsert_social_post_cache(posts)

        all_aapl = self.storage.get_social_posts_for_ticker("AAPL")
        self.assertEqual(len(all_aapl), 2)

        recent_aapl = self.storage.get_social_posts_for_ticker(
            "AAPL", since=now - timedelta(days=10)
        )
        self.assertEqual(len(recent_aapl), 1)
        self.assertEqual(recent_aapl[0].post_id, "recent-1")

        msft_posts = self.storage.get_social_posts_for_ticker("MSFT")
        self.assertEqual(len(msft_posts), 1)
        self.assertEqual(msft_posts[0].post_id, "msft-1")

    def test_purge_old_social_posts_returns_correct_count(self) -> None:
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(days=20)
        recent_time = now - timedelta(days=5)

        posts = [
            SocialPostCacheRow(
                source="reddit",
                post_id="old-1",
                ticker="AAPL",
                posted_at=old_time,
                title="Old Post",
                sentiment="bull",
                confidence=0.8,
                one_line_summary="Old",
                engagement_score=10.0,
                ingested_at=old_time,
            ),
            SocialPostCacheRow(
                source="reddit",
                post_id="old-2",
                ticker="MSFT",
                posted_at=old_time,
                title="Old Post 2",
                sentiment="bull",
                confidence=0.8,
                one_line_summary="Old",
                engagement_score=10.0,
                ingested_at=old_time,
            ),
            SocialPostCacheRow(
                source="x",
                post_id="recent-1",
                ticker="AAPL",
                posted_at=recent_time,
                title="Recent Post",
                sentiment="bear",
                confidence=0.9,
                one_line_summary="Recent",
                engagement_score=50.0,
                ingested_at=recent_time,
            ),
        ]
        self.storage.upsert_social_post_cache(posts)

        before_count = len(self.storage.get_social_posts_for_ticker("AAPL"))
        self.assertEqual(before_count, 2)

        cutoff = now - timedelta(days=10)
        deleted = self.storage.purge_old_social_posts(cutoff)
        self.assertEqual(deleted, 2)

        after_count = len(self.storage.get_social_posts_for_ticker("AAPL"))
        self.assertEqual(after_count, 1)

    def test_upsert_filing_summary_cache_is_idempotent(self) -> None:
        now = datetime.now(timezone.utc)
        filing1 = FilingSummaryCacheRow(
            cik="1018724",
            accession_number="0000320193-23-000077",
            ticker="AAPL",
            form_type="10-K",
            filed_at=now,
            period_end=now,
            summary="Revenue $383B, strong margins",
            sentiment="bull",
            key_metrics_json='{"revenue": 383000000000, "cash": 100000000000}',
            ingested_at=now,
        )

        self.storage.upsert_filing_summary_cache([filing1])
        rows1 = self.storage.get_filing_summaries_for_ticker("AAPL")
        self.assertEqual(len(rows1), 1)
        self.assertEqual(rows1[0].sentiment, "bull")

        filing2 = FilingSummaryCacheRow(
            cik="1018724",
            accession_number="0000320193-23-000077",
            ticker="AAPL",
            form_type="10-K",
            filed_at=now,
            period_end=now,
            summary="Revenue $383B, strong margins",
            sentiment="bear",
            key_metrics_json='{"revenue": 383000000000, "cash": 100000000000}',
            ingested_at=now,
        )

        self.storage.upsert_filing_summary_cache([filing2])
        rows2 = self.storage.get_filing_summaries_for_ticker("AAPL")
        self.assertEqual(len(rows2), 1)
        self.assertEqual(rows2[0].sentiment, "bull")

    def test_get_filing_summaries_filters_by_ticker_and_form_types(self) -> None:
        now = datetime.now(timezone.utc)

        filings = [
            FilingSummaryCacheRow(
                cik="1018724",
                accession_number="0000320193-23-000077",
                ticker="AAPL",
                form_type="10-K",
                filed_at=now,
                period_end=now,
                summary="Annual report",
                sentiment="bull",
                key_metrics_json="{}",
                ingested_at=now,
            ),
            FilingSummaryCacheRow(
                cik="1018724",
                accession_number="0000320193-23-000100",
                ticker="AAPL",
                form_type="10-Q",
                filed_at=now - timedelta(days=45),
                period_end=now - timedelta(days=45),
                summary="Quarterly report",
                sentiment="neutral",
                key_metrics_json="{}",
                ingested_at=now - timedelta(days=45),
            ),
            FilingSummaryCacheRow(
                cik="789019",
                accession_number="0000066740-23-000025",
                ticker="MSFT",
                form_type="10-K",
                filed_at=now,
                period_end=now,
                summary="Microsoft annual",
                sentiment="bull",
                key_metrics_json="{}",
                ingested_at=now,
            ),
        ]
        self.storage.upsert_filing_summary_cache(filings)

        all_aapl = self.storage.get_filing_summaries_for_ticker("AAPL")
        self.assertEqual(len(all_aapl), 2)

        aapl_10k = self.storage.get_filing_summaries_for_ticker("AAPL", form_types=["10-K"])
        self.assertEqual(len(aapl_10k), 1)
        self.assertEqual(aapl_10k[0].form_type, "10-K")

        aapl_10q = self.storage.get_filing_summaries_for_ticker("AAPL", form_types=["10-Q"])
        self.assertEqual(len(aapl_10q), 1)
        self.assertEqual(aapl_10q[0].form_type, "10-Q")

        msft_all = self.storage.get_filing_summaries_for_ticker("MSFT")
        self.assertEqual(len(msft_all), 1)
        self.assertEqual(msft_all[0].ticker, "MSFT")

    def test_purge_old_filing_summaries_returns_correct_count(self) -> None:
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(days=200)
        recent_time = now - timedelta(days=10)

        filings = [
            FilingSummaryCacheRow(
                cik="1018724",
                accession_number="0000320193-22-000001",
                ticker="AAPL",
                form_type="10-K",
                filed_at=old_time,
                period_end=old_time,
                summary="Old annual",
                sentiment="bull",
                key_metrics_json="{}",
                ingested_at=old_time,
            ),
            FilingSummaryCacheRow(
                cik="1018724",
                accession_number="0000320193-23-000077",
                ticker="AAPL",
                form_type="10-K",
                filed_at=recent_time,
                period_end=recent_time,
                summary="Recent annual",
                sentiment="bull",
                key_metrics_json="{}",
                ingested_at=recent_time,
            ),
        ]
        self.storage.upsert_filing_summary_cache(filings)

        before_count = len(self.storage.get_filing_summaries_for_ticker("AAPL"))
        self.assertEqual(before_count, 2)

        cutoff = now - timedelta(days=100)
        deleted = self.storage.purge_old_filing_summaries(cutoff)
        self.assertEqual(deleted, 1)

        after_count = len(self.storage.get_filing_summaries_for_ticker("AAPL"))
        self.assertEqual(after_count, 1)
