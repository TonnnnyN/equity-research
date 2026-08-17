from __future__ import annotations

import logging
from datetime import date

from market_sentiment.config import ProjectConfig
from market_sentiment.http import HttpClient
from market_sentiment.models import PipelineContext
from market_sentiment.sources.social_base import SocialCollectionResult
from market_sentiment.storage import Storage

logger = logging.getLogger(__name__)


class SocialSignalService:
    """Social signal service stub: returns empty results.

    Social data collection and scoring has been moved to the agent tier.
    The agent fetches social URLs directly using its own web tools and reads
    posts without relying on paid APIs. Review packets provide social_sources_to_fetch
    with pre-built search URLs for each triggered ticker.

    This class remains for compatibility but returns empty results so scoring.py
    handles the missing social data gracefully (max_score=0, no penalties).
    """

    def __init__(
        self,
        *,
        config: ProjectConfig,
        http: HttpClient,
        storage: Storage,
    ) -> None:
        self.config = config
        self.storage = storage
        self.http = http

    def collect(self, context: PipelineContext, run_date: date) -> SocialCollectionResult:
        """Return empty social result; agent fetches URLs directly."""
        return SocialCollectionResult(snapshot=None, posts=[], statuses=[])
