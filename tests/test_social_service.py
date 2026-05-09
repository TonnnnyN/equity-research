from __future__ import annotations

import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from market_sentiment.config import load_config
from market_sentiment.models import PipelineContext, Security, SocialPost, SourceStatus
from market_sentiment.social_service import SocialSignalService
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.social_base import SocialProvider
from market_sentiment.storage import Storage


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "watchlist.toml"


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


class ExplodingProvider(SocialProvider):
    def __init__(self, name: str) -> None:
        self.name = name

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
        raise RuntimeError("provider crashed")


class TimeoutAwareSlowProvider(SocialProvider):
    def __init__(self, name: str, delay_seconds: float) -> None:
        self.name = name
        self.delay_seconds = delay_seconds
        self.timeout_seen: float | None = None

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
        self.timeout_seen = timeout_seconds
        if timeout_seconds is not None and timeout_seconds < self.delay_seconds:
            time.sleep(timeout_seconds / 4)
            raise TimeoutError(f"{self.name} fetch timed out after {timeout_seconds:.2f}s")
        time.sleep(self.delay_seconds)
        return SourcePayload(
            data=[],
            status=SourceStatus(source=self.name, success=True, message="slow ok"),
        )


def make_post(author: str, hours_ago: int) -> SocialPost:
    created_at = datetime(2026, 3, 26, 16, 0, 0) - timedelta(hours=hours_ago)
    return SocialPost(
        ticker="MSFT",
        source="reddit",
        community="stocks",
        post_id=f"post-{author}-{hours_ago}",
        created_at=created_at,
        title=f"MSFT demand improving with stronger backlog {author}",
        body="Cloud demand and cash flow both look healthier after the selloff.",
        url=f"https://example.com/{author}",
        author_handle=author,
        author_id_hash=author,
        engagement_score=12.0,
        matched_text=True,
    )


class SocialServiceTests(TestCase):
    def test_collect_survives_optional_provider_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(str(CONFIG_PATH))
            config.social.enabled = True
            config.social.providers = ["reddit", "x"]
            config.social.min_recent_posts = 3
            config.social.min_unique_authors = 2
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            security = Security(ticker="MSFT", name="Microsoft Corporation", layer=config.securities[0].layer, benchmark="QQQ")
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
            service = SocialSignalService(
                config=config,
                http=None,  # type: ignore[arg-type]
                storage=storage,
                providers=[
                    StubProvider(
                        "reddit",
                        SourcePayload(
                            data=[
                                make_post("bull-1", 8),
                                make_post("bull-2", 12),
                                make_post("bull-3", 18),
                            ],
                            status=SourceStatus(source="reddit", success=True, message="ok"),
                        ),
                    ),
                    StubProvider(
                        "x",
                        SourcePayload(
                            data=[],
                            status=SourceStatus(source="x", success=False, partial=True, message="x unavailable"),
                        ),
                    ),
                ],
            )

            result = service.collect(context, date(2026, 3, 26))

            self.assertIsNotNone(result.snapshot)
            self.assertEqual(len(result.statuses), 2)
            self.assertTrue(any(status.source == "x" and not status.success for status in result.statuses))
            self.assertGreaterEqual(len(result.posts), 1)

    def test_collect_converts_provider_exception_into_partial_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(str(CONFIG_PATH))
            config.social.enabled = True
            config.social.providers = ["reddit", "x"]
            config.social.min_recent_posts = 2
            config.social.min_unique_authors = 2
            config.social.max_posts_per_source = 2
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            security = Security(ticker="MSFT", name="Microsoft Corporation", layer=config.securities[0].layer, benchmark="QQQ")
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
            service = SocialSignalService(
                config=config,
                http=None,  # type: ignore[arg-type]
                storage=storage,
                providers=[
                    StubProvider(
                        "reddit",
                        SourcePayload(
                            data=[
                                make_post("bull-1", 8),
                                make_post("bull-2", 12),
                                make_post("bull-3", 18),
                            ],
                            status=SourceStatus(source="reddit", success=True, message="ok"),
                        ),
                    ),
                    ExplodingProvider("x"),
                ],
            )

            result = service.collect(context, date(2026, 3, 26))

            self.assertIsNotNone(result.snapshot)
            self.assertEqual(len(result.statuses), 2)
            self.assertTrue(any(status.source == "x" and not status.success for status in result.statuses))
            self.assertEqual(len(result.posts), 2)

    def test_collect_builds_snapshot_from_partial_data_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(str(CONFIG_PATH))
            config.social.enabled = True
            config.social.providers = ["x"]
            config.social.min_recent_posts = 2
            config.social.min_unique_authors = 2
            config.social.max_posts_per_source = 2
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            security = Security(ticker="MSFT", name="Microsoft Corporation", layer=config.securities[0].layer, benchmark="QQQ")
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
            service = SocialSignalService(
                config=config,
                http=None,  # type: ignore[arg-type]
                storage=storage,
                providers=[
                    StubProvider(
                        "x",
                        SourcePayload(
                            data=[
                                make_post("bull-1", 8),
                                make_post("bull-2", 12),
                            ],
                            status=SourceStatus(source="x", success=False, partial=True, message="x partial"),
                        ),
                    ),
                ],
            )

            result = service.collect(context, date(2026, 3, 26))

            self.assertIsNotNone(result.snapshot)
            self.assertEqual(result.snapshot.provider_count, 0)
            self.assertEqual(len(result.statuses), 1)
            self.assertIn("social_source_count_insufficient", result.snapshot.notes)

    def test_collect_dedupes_duplicate_posts_before_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(str(CONFIG_PATH))
            config.social.enabled = True
            config.social.providers = ["reddit"]
            config.social.min_recent_posts = 1
            config.social.min_unique_authors = 1
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            security = Security(ticker="MSFT", name="Microsoft Corporation", layer=config.securities[0].layer, benchmark="QQQ")
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
            duplicate_post = make_post("bull-1", 8)
            service = SocialSignalService(
                config=config,
                http=None,  # type: ignore[arg-type]
                storage=storage,
                providers=[
                    StubProvider(
                        "reddit",
                        SourcePayload(
                            data=[duplicate_post, duplicate_post],
                            status=SourceStatus(source="reddit", success=True, message="ok"),
                        ),
                    ),
                ],
            )

            result = service.collect(context, date(2026, 3, 26))

            self.assertIsNotNone(result.snapshot)
            self.assertEqual(result.snapshot.total_posts, 1)

    def test_collect_passes_project_timezone_into_snapshot_bucketing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(str(CONFIG_PATH))
            config.timezone = "America/New_York"
            config.social.enabled = True
            config.social.providers = ["reddit"]
            config.social.min_recent_posts = 1
            config.social.min_unique_authors = 1
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            security = Security(ticker="MSFT", name="Microsoft Corporation", layer=config.securities[0].layer, benchmark="QQQ")
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
            late_local_post = SocialPost(
                ticker="MSFT",
                source="reddit",
                community="stocks",
                post_id="late-local-post",
                created_at=datetime(2026, 3, 27, 3, 30, tzinfo=timezone.utc),
                title="Microsoft demand improving late in the New York session",
                body="Backlog and cash flow both look stronger.",
                url="https://example.com/late-local-post",
                author_handle="late-bull",
                author_id_hash="late-bull",
                engagement_score=14.0,
                matched_text=True,
            )
            service = SocialSignalService(
                config=config,
                http=None,  # type: ignore[arg-type]
                storage=storage,
                providers=[
                    StubProvider(
                        "reddit",
                        SourcePayload(
                            data=[late_local_post],
                            status=SourceStatus(source="reddit", success=True, message="ok"),
                        ),
                    ),
                ],
            )

            result = service.collect(context, date(2026, 3, 26))

            self.assertIsNotNone(result.snapshot)
            self.assertEqual(result.snapshot.recent_posts, 1)

    def test_collect_delegates_provider_timeout_without_blocking_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(str(CONFIG_PATH))
            config.social.enabled = True
            config.social.providers = ["reddit", "x"]
            config.social.provider_timeout_seconds = 0.01
            config.social.min_recent_posts = 2
            config.social.min_unique_authors = 2
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            security = Security(ticker="MSFT", name="Microsoft Corporation", layer=config.securities[0].layer, benchmark="QQQ")
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
            slow_provider = TimeoutAwareSlowProvider("x", 0.1)
            service = SocialSignalService(
                config=config,
                http=None,  # type: ignore[arg-type]
                storage=storage,
                providers=[
                    StubProvider(
                        "reddit",
                        SourcePayload(
                            data=[make_post("bull-1", 8), make_post("bull-2", 12)],
                            status=SourceStatus(source="reddit", success=True, message="ok"),
                        ),
                    ),
                    slow_provider,
                ],
            )

            started = time.monotonic()
            result = service.collect(context, date(2026, 3, 26))
            elapsed = time.monotonic() - started

            self.assertIsNotNone(result.snapshot)
            self.assertLess(elapsed, 0.08)
            self.assertEqual(slow_provider.timeout_seen, 0.01)
            self.assertTrue(any("timed out" in status.message for status in result.statuses if status.source == "x"))
