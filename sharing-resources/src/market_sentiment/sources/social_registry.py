from __future__ import annotations

from market_sentiment.config import SocialConfig
from market_sentiment.http import HttpClient
from market_sentiment.sources.discourse import DiscourseClient
from market_sentiment.sources.reddit import RedditClient
from market_sentiment.sources.social_base import SocialProvider
from market_sentiment.sources.x_provider import XClient
from market_sentiment.storage import Storage


def build_social_providers(
    *,
    http: HttpClient,
    storage: Storage,
    config: SocialConfig,
) -> list[SocialProvider]:
    return [
        RedditClient(http, storage, config),
        DiscourseClient(http, storage, config),
        XClient(config, storage=storage),
    ]
