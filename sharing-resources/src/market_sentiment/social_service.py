from __future__ import annotations

from datetime import date

from market_sentiment.config import ProjectConfig
from market_sentiment.http import HttpClient
from market_sentiment.models import PipelineContext, SourceStatus
from market_sentiment.social_rebound import annotate_posts, build_social_snapshot
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.social_base import SocialCollectionResult, SocialProvider, dedupe_social_posts
from market_sentiment.sources.social_registry import build_social_providers
from market_sentiment.storage import Storage


class SocialSignalService:
    def __init__(
        self,
        *,
        config: ProjectConfig,
        http: HttpClient,
        storage: Storage,
        providers: list[SocialProvider] | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.providers = providers or build_social_providers(http=http, storage=storage, config=config.social)

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
            posts=snapshot.representative_posts or annotated_posts[:3],
            statuses=statuses,
        )


def _cap_posts(posts, limit: int):
    if limit <= 0:
        return []
    ordered = sorted(posts, key=lambda item: (item.created_at, item.engagement_score), reverse=True)
    return ordered[:limit]
