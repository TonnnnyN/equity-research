from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from market_sentiment.config import load_config
from market_sentiment.models import PipelineContext, Security, SocialPost, SourceStatus
from market_sentiment.social_service import SocialSignalService
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.social_base import SocialProvider
from market_sentiment.storage import Storage
from market_sentiment.subagent_sentiment import PostToJudge, SentimentJudge, SentimentJudgement


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "watchlist.toml"


class StubProvider(SocialProvider):
    def __init__(self, name: str, payload: SourcePayload[list[SocialPost]]) -> None:
        self.name = name
        self._payload = payload

    def is_enabled(self) -> bool:
        return True

    def fetch_posts(
        self,
        ticker: str,
        company_name: str,
        run_date: date,
        *,
        timeout_seconds: float | None = None,
    ) -> SourcePayload[list[SocialPost]]:
        return self._payload


class MockJudge(SentimentJudge):
    def __init__(self):
        self.calls: list[list[PostToJudge]] = []

    def judge_batch(self, posts: list[PostToJudge]) -> list[SentimentJudgement]:
        self.calls.append(list(posts))
        return [
            SentimentJudgement(
                source=p.source,
                post_id=p.post_id,
                ticker=p.ticker,
                sentiment="bull",
                confidence=0.9,
                one_line_summary=f"Test judgment for {p.post_id[:20]}",
            )
            for p in posts
        ]


def make_post(post_id: str, hours_ago: int, engagement: float = 10.0) -> SocialPost:
    created_at = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc) - timedelta(hours=hours_ago)
    return SocialPost(
        ticker="AAPL",
        source="reddit",
        community="stocks",
        post_id=post_id,
        created_at=created_at,
        title=f"AAPL news: {post_id}",
        body=f"Full body text for post {post_id}. This contains detailed content.",
        url=f"https://example.com/{post_id}",
        author_handle="testauthor",
        author_id_hash="testhash",
        engagement_score=engagement,
        matched_text=True,
    )


class SocialCacheIntegrationTests(TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name)
        self.storage = Storage(self.data_dir / "test.db", self.data_dir)
        self.storage.init_db()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_first_collect_sends_all_posts_to_judge(self) -> None:
        """First collect: all posts are new, all sent to judge."""
        config = load_config(str(CONFIG_PATH))
        config.social.enabled = True
        config.social.providers = ["reddit"]
        config.social.min_recent_posts = 1
        config.social.min_unique_authors = 1
        security = Security(
            ticker="AAPL",
            name="Apple Inc",
            layer=config.securities[0].layer,
            benchmark="QQQ",
        )
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
        )

        posts = [make_post(f"post-{i}", i * 2) for i in range(3)]
        mock_judge = MockJudge()

        service = SocialSignalService(
            config=config,
            http=None,  # type: ignore[arg-type]
            storage=self.storage,
            providers=[
                StubProvider(
                    "reddit",
                    SourcePayload(
                        data=posts,
                        status=SourceStatus(source="reddit", success=True, message="ok"),
                    ),
                ),
            ],
            sentiment_judge=mock_judge,
        )

        result = service.collect(context, date(2026, 5, 12))

        # All 3 posts should be sent to judge
        self.assertEqual(len(mock_judge.calls), 1)
        self.assertEqual(len(mock_judge.calls[0]), 3)
        self.assertIsNotNone(result.snapshot)

    def test_second_collect_same_posts_skips_judge(self) -> None:
        """Second collect of same posts: judge not called, posts read from cache."""
        config = load_config(str(CONFIG_PATH))
        config.social.enabled = True
        config.social.providers = ["reddit"]
        config.social.min_recent_posts = 1
        config.social.min_unique_authors = 1
        security = Security(
            ticker="AAPL",
            name="Apple Inc",
            layer=config.securities[0].layer,
            benchmark="QQQ",
        )
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
        )

        posts = [make_post(f"post-{i}", i * 2) for i in range(3)]

        # First collect
        mock_judge_1 = MockJudge()
        service_1 = SocialSignalService(
            config=config,
            http=None,  # type: ignore[arg-type]
            storage=self.storage,
            providers=[
                StubProvider(
                    "reddit",
                    SourcePayload(
                        data=posts,
                        status=SourceStatus(source="reddit", success=True, message="ok"),
                    ),
                ),
            ],
            sentiment_judge=mock_judge_1,
        )
        service_1.collect(context, date(2026, 5, 12))

        # Second collect of same posts
        mock_judge_2 = MockJudge()
        service_2 = SocialSignalService(
            config=config,
            http=None,  # type: ignore[arg-type]
            storage=self.storage,
            providers=[
                StubProvider(
                    "reddit",
                    SourcePayload(
                        data=posts,
                        status=SourceStatus(source="reddit", success=True, message="ok"),
                    ),
                ),
            ],
            sentiment_judge=mock_judge_2,
        )
        result = service_2.collect(context, date(2026, 5, 12))

        # Judge should NOT be called on second collect
        self.assertEqual(len(mock_judge_2.calls), 0)
        self.assertIsNotNone(result.snapshot)

    def test_mixed_old_and_new_posts_judges_only_new(self) -> None:
        """Mixed scenario: 5 cached + 3 new, judge called only for 3 new."""
        config = load_config(str(CONFIG_PATH))
        config.social.enabled = True
        config.social.providers = ["reddit"]
        config.social.min_recent_posts = 1
        config.social.min_unique_authors = 1
        security = Security(
            ticker="AAPL",
            name="Apple Inc",
            layer=config.securities[0].layer,
            benchmark="QQQ",
        )
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
        )

        # First collect: 5 old posts
        old_posts = [make_post(f"old-post-{i}", 20 + i) for i in range(5)]
        mock_judge_1 = MockJudge()
        service_1 = SocialSignalService(
            config=config,
            http=None,  # type: ignore[arg-type]
            storage=self.storage,
            providers=[
                StubProvider(
                    "reddit",
                    SourcePayload(
                        data=old_posts,
                        status=SourceStatus(source="reddit", success=True, message="ok"),
                    ),
                ),
            ],
            sentiment_judge=mock_judge_1,
        )
        service_1.collect(context, date(2026, 5, 12))
        self.assertEqual(len(mock_judge_1.calls[0]), 5)

        # Second collect: 5 old + 3 new
        new_posts = [make_post(f"new-post-{i}", i) for i in range(3)]
        combined_posts = old_posts + new_posts

        mock_judge_2 = MockJudge()
        service_2 = SocialSignalService(
            config=config,
            http=None,  # type: ignore[arg-type]
            storage=self.storage,
            providers=[
                StubProvider(
                    "reddit",
                    SourcePayload(
                        data=combined_posts,
                        status=SourceStatus(source="reddit", success=True, message="ok"),
                    ),
                ),
            ],
            sentiment_judge=mock_judge_2,
        )
        result = service_2.collect(context, date(2026, 5, 12))

        # Only 3 new posts should be judged
        self.assertEqual(len(mock_judge_2.calls), 1)
        self.assertEqual(len(mock_judge_2.calls[0]), 3)
        self.assertIsNotNone(result.snapshot)

    def test_output_does_not_contain_body_field(self) -> None:
        """Output posts should not have body field, only title + summary."""
        config = load_config(str(CONFIG_PATH))
        config.social.enabled = True
        config.social.providers = ["reddit"]
        config.social.min_recent_posts = 1
        config.social.min_unique_authors = 1
        security = Security(
            ticker="AAPL",
            name="Apple Inc",
            layer=config.securities[0].layer,
            benchmark="QQQ",
        )
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
        )

        posts = [make_post(f"post-{i}", i * 2) for i in range(2)]
        mock_judge = MockJudge()

        service = SocialSignalService(
            config=config,
            http=None,  # type: ignore[arg-type]
            storage=self.storage,
            providers=[
                StubProvider(
                    "reddit",
                    SourcePayload(
                        data=posts,
                        status=SourceStatus(source="reddit", success=True, message="ok"),
                    ),
                ),
            ],
            sentiment_judge=mock_judge,
        )

        result = service.collect(context, date(2026, 5, 12))

        # Verify output posts don't have body field
        for post in result.posts:
            # _OutputPost doesn't expose body, but verify it has sentiment and summary
            self.assertTrue(hasattr(post, "sentiment"))
            self.assertTrue(hasattr(post, "one_line_summary"))
            self.assertTrue(hasattr(post, "confidence"))
            self.assertFalse(hasattr(post, "body"))

    def test_cache_survives_across_service_instances(self) -> None:
        """Cache written by first service should be readable by second."""
        config = load_config(str(CONFIG_PATH))
        config.social.enabled = True
        config.social.providers = ["reddit"]
        config.social.min_recent_posts = 1
        config.social.min_unique_authors = 1
        security = Security(
            ticker="AAPL",
            name="Apple Inc",
            layer=config.securities[0].layer,
            benchmark="QQQ",
        )
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
        )

        posts = [make_post(f"post-{i}", i) for i in range(2)]

        # First service
        service_1 = SocialSignalService(
            config=config,
            http=None,  # type: ignore[arg-type]
            storage=self.storage,
            providers=[
                StubProvider(
                    "reddit",
                    SourcePayload(
                        data=posts,
                        status=SourceStatus(source="reddit", success=True, message="ok"),
                    ),
                ),
            ],
            sentiment_judge=MockJudge(),
        )
        service_1.collect(context, date(2026, 5, 12))

        # Verify cache was written
        cached = self.storage.get_social_posts_for_ticker("AAPL")
        self.assertEqual(len(cached), 2)

        # Second service should read same cache
        mock_judge_2 = MockJudge()
        service_2 = SocialSignalService(
            config=config,
            http=None,  # type: ignore[arg-type]
            storage=self.storage,
            providers=[
                StubProvider(
                    "reddit",
                    SourcePayload(
                        data=posts,
                        status=SourceStatus(source="reddit", success=True, message="ok"),
                    ),
                ),
            ],
            sentiment_judge=mock_judge_2,
        )
        service_2.collect(context, date(2026, 5, 12))

        # Second judge should not be called
        self.assertEqual(len(mock_judge_2.calls), 0)

    def test_collect_gracefully_handles_judge_exception(self) -> None:
        """When judge_batch raises, collect returns posts with sentiment='unknown' without crashing."""
        config = load_config(str(CONFIG_PATH))
        config.social.enabled = True
        config.social.providers = ["reddit"]
        config.social.min_recent_posts = 1
        config.social.min_unique_authors = 1
        security = Security(
            ticker="AAPL",
            name="Apple Inc",
            layer=config.securities[0].layer,
            benchmark="QQQ",
        )
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
        )

        posts = [make_post(f"post-{i}", i * 2) for i in range(3)]

        # Create a judge that raises an exception
        class FailingJudge(SentimentJudge):
            def judge_batch(self, posts: list[PostToJudge]) -> list[SentimentJudgement]:
                raise RuntimeError("Simulated judge failure")

        service = SocialSignalService(
            config=config,
            http=None,  # type: ignore[arg-type]
            storage=self.storage,
            providers=[
                StubProvider(
                    "reddit",
                    SourcePayload(
                        data=posts,
                        status=SourceStatus(source="reddit", success=True, message="ok"),
                    ),
                ),
            ],
            sentiment_judge=FailingJudge(),
        )

        # Should not raise, should return gracefully
        result = service.collect(context, date(2026, 5, 12))

        # Verify result is not None and has posts
        self.assertIsNotNone(result.snapshot)
        self.assertGreater(len(result.posts), 0)

        # Verify all returned posts have sentiment="unknown" and confidence=0.0
        for post in result.posts:
            self.assertEqual(post.sentiment, "unknown")
            self.assertEqual(post.confidence, 0.0)

        # Verify cache is empty (no judgments were cached)
        cached = self.storage.get_social_posts_for_ticker("AAPL")
        self.assertEqual(len(cached), 0)
