from __future__ import annotations

from equity_research.config import SocialConfig
from equity_research.http import HttpClient
from equity_research.sources.discourse import DiscourseClient
from equity_research.sources.reddit import RedditClient
from equity_research.sources.social_base import SocialProvider
from equity_research.sources.x_provider import XClient
from equity_research.storage import Storage


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
