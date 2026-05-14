from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from market_sentiment.config import ProjectConfig
from market_sentiment.http import HttpClient
from market_sentiment.models import PipelineContext, SocialPost, SocialPostCacheRow, SourceStatus
from market_sentiment.social_rebound import annotate_posts, build_social_snapshot
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.social_base import SocialCollectionResult, SocialProvider, dedupe_social_posts
from market_sentiment.sources.social_registry import build_social_providers
from market_sentiment.storage import Storage
from market_sentiment.subagent_sentiment import (
    PostToJudge,
    SentimentJudge,
    SentimentJudgement,
    StubSentimentJudge,
)

logger = logging.getLogger(__name__)

_BODY_CHAR_CAP = 600


class SocialSignalService:
    def __init__(
        self,
        *,
        config: ProjectConfig,
        http: HttpClient,
        storage: Storage,
        providers: list[SocialProvider] | None = None,
        sentiment_judge: SentimentJudge | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.providers = providers or build_social_providers(http=http, storage=storage, config=config.social)
        self.sentiment_judge = sentiment_judge or StubSentimentJudge()

    def collect(self, context: PipelineContext, run_date: date) -> SocialCollectionResult:
        if not self.config.social.enabled:
            return SocialCollectionResult(snapshot=None, posts=[], statuses=[])

        statuses = []
        collected_posts = []
        successful_providers: set[str] = set()
        active_provider_seen = False

        for provider in self.providers:
            if provider.name not in self.config.social.providers:
                continue
            if not provider.is_enabled():
                continue
            active_provider_seen = True
            try:
                payload = provider.fetch_posts(
                    context.security.ticker,
                    context.security.name,
                    run_date,
                    timeout_seconds=self.config.social.provider_timeout_seconds,
                )
            except Exception as exc:
                statuses.append(
                    SourceStatus(
                        source=provider.name,
                        success=False,
                        partial=True,
                        message=f"{provider.name} fetch failed: {exc}",
                    )
                )
                continue
            statuses.append(payload.status)
            if payload.status.success and payload.data:
                successful_providers.add(provider.name)
            if payload.data:
                collected_posts.extend(_cap_posts(payload.data, self.config.social.max_posts_per_source))

        if not active_provider_seen:
            return SocialCollectionResult(snapshot=None, posts=[], statuses=[])

        annotated_posts = annotate_posts(dedupe_social_posts(collected_posts))
        self.storage.upsert_social_posts(annotated_posts)

        if not any(status.success for status in statuses) and not annotated_posts:
            return SocialCollectionResult(snapshot=None, posts=annotated_posts, statuses=statuses)

        # Apply cache-aware sentiment judgment
        output_posts = self._judge_posts_with_cache(annotated_posts, context.security.ticker)

        snapshot = build_social_snapshot(
            ticker=context.security.ticker,
            run_date=run_date,
            posts=annotated_posts,
            min_informative_posts=self.config.social.min_recent_posts,
            min_unique_authors=self.config.social.min_unique_authors,
            min_sources=self.config.social.min_platform_count,
            max_author_share=self.config.social.max_author_share,
            recent_window_hours=self.config.social.lookback_hours,
            baseline_days=self.config.social.baseline_days,
            provider_names=successful_providers,
            timezone_name=self.config.timezone,
        )
        self.storage.upsert_social_snapshot(snapshot)
        return SocialCollectionResult(
            snapshot=snapshot,
            posts=output_posts or (snapshot.representative_posts or annotated_posts[:3]),
            statuses=statuses,
        )


    def _judge_posts_with_cache(self, posts: list[SocialPost], ticker: str) -> list[_OutputPost]:
        """Judge posts using cache and subagent, avoiding re-judgment of cached posts.

        1. Query cache for posts we've already judged (last 14 days)
        2. Split current posts into cached (skip subagent) and new (send to judge)
        3. Call judge_batch for new posts, write results to cache
        4. Merge cached + new judgments, return output (without body field)
        """
        now = datetime.now(timezone.utc)
        lookback = now - timedelta(days=14)

        # Read cached posts from storage
        cached_rows = self.storage.get_social_posts_for_ticker(ticker, since=lookback)
        cached_ids = {(row.source, row.post_id) for row in cached_rows}

        # Split posts into cached and new
        new_posts = [p for p in posts if (p.source, p.post_id) not in cached_ids]

        # Build dict for O(1) lookup of original posts
        posts_by_key = {(p.source, p.post_id): p for p in new_posts}

        # Judge only new posts
        new_judgements: list[SentimentJudgement] = []
        if new_posts:
            posts_to_judge = [
                PostToJudge(
                    source=p.source,
                    post_id=p.post_id,
                    ticker=p.ticker,
                    posted_at=p.created_at,
                    title=p.title,
                    body=p.body,
                    engagement_score=p.engagement_score,
                )
                for p in new_posts
            ]
            try:
                new_judgements = self.sentiment_judge.judge_batch(posts_to_judge)
            except Exception as exc:
                logger.warning(f"judge_batch failed for {ticker}: {exc}. Returning posts with unknown sentiment.")
                new_judgements = []

            # Write new judgments to cache (skip stub judgments)
            if new_judgements:
                cache_rows = [
                    SocialPostCacheRow(
                        source=j.source,
                        post_id=j.post_id,
                        ticker=j.ticker,
                        posted_at=posts_by_key[(j.source, j.post_id)].created_at,
                        title=posts_by_key[(j.source, j.post_id)].title,
                        sentiment=j.sentiment,
                        confidence=j.confidence,
                        one_line_summary=j.one_line_summary,
                        engagement_score=posts_by_key[(j.source, j.post_id)].engagement_score,
                        ingested_at=now,
                    )
                    for j in new_judgements
                    if not j.is_stub
                ]
                if cache_rows:
                    self.storage.upsert_social_post_cache(cache_rows)
                logger.debug(f"Judged {len(new_judgements)} new posts for {ticker}")
            else:
                logger.debug(f"No judgments obtained for {len(new_posts)} new posts for {ticker}")
        else:
            logger.debug(f"All {len(posts)} posts for {ticker} already cached, skipping judgment")

        # Build output combining cached and new
        output_by_id: dict[tuple[str, str], _OutputPost] = {}

        # Add cached posts
        for cached_row in cached_rows:
            output_by_id[(cached_row.source, cached_row.post_id)] = _OutputPost(
                source=cached_row.source,
                post_id=cached_row.post_id,
                posted_at=cached_row.posted_at,
                title=cached_row.title,
                sentiment=cached_row.sentiment,
                confidence=cached_row.confidence,
                one_line_summary=cached_row.one_line_summary,
                engagement_score=cached_row.engagement_score,
            )

        # Add new judgments
        for judgment in new_judgements:
            orig_post = posts_by_key[(judgment.source, judgment.post_id)]
            output_by_id[(judgment.source, judgment.post_id)] = _OutputPost(
                source=judgment.source,
                post_id=judgment.post_id,
                posted_at=orig_post.created_at,
                title=judgment.one_line_summary,
                sentiment=judgment.sentiment,
                confidence=judgment.confidence,
                one_line_summary=judgment.one_line_summary,
                engagement_score=orig_post.engagement_score,
            )

        # Add new posts that weren't judged (fallback for judge failure)
        for post_key, orig_post in posts_by_key.items():
            if post_key not in output_by_id:
                output_by_id[post_key] = _OutputPost(
                    source=orig_post.source,
                    post_id=orig_post.post_id,
                    posted_at=orig_post.created_at,
                    title=orig_post.title,
                    sentiment="unknown",
                    confidence=0.0,
                    one_line_summary=orig_post.title,
                    engagement_score=orig_post.engagement_score,
                )

        # Sort by engagement score and apply cap
        result = sorted(output_by_id.values(), key=lambda p: p.engagement_score, reverse=True)
        return _cap_output_posts(result, self.config.social.max_posts_per_source)


def _cap_posts(posts, limit: int):
    if limit <= 0:
        return []
    ordered = sorted(posts, key=lambda item: (item.created_at, item.engagement_score), reverse=True)
    capped = ordered[:limit]

    # Truncate body to _BODY_CHAR_CAP characters
    result = []
    for post in capped:
        if len(post.body) > _BODY_CHAR_CAP:
            from dataclasses import replace
            post = replace(post, body=post.body[:_BODY_CHAR_CAP] + "...[truncated]")
        result.append(post)
    return result


@dataclass(slots=True)
class _OutputPost:
    """Internal output post format (without full body)."""

    source: str
    post_id: str
    posted_at: datetime
    title: str
    sentiment: str
    confidence: float
    one_line_summary: str
    engagement_score: float


def _cap_output_posts(posts: list[_OutputPost], limit: int) -> list[_OutputPost]:
    if limit <= 0:
        return []
    return posts[:limit]
