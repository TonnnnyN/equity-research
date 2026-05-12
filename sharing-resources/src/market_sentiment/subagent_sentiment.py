from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(slots=True)
class PostToJudge:
    source: str
    post_id: str
    ticker: str
    posted_at: datetime
    title: str
    body: str
    engagement_score: float


@dataclass(slots=True)
class SentimentJudgement:
    source: str
    post_id: str
    ticker: str
    sentiment: str
    confidence: float
    one_line_summary: str
    is_stub: bool = False


class SentimentJudge(Protocol):
    def judge_batch(self, posts: list[PostToJudge]) -> list[SentimentJudgement]:
        ...


class StubSentimentJudge:
    """Stub implementation: returns neutral for all posts.

    Real Haiku subagent integration is P0 step 3.
    TODO: Replace with actual Anthropic API call in next step.
    """

    def judge_batch(self, posts: list[PostToJudge]) -> list[SentimentJudgement]:
        return [
            SentimentJudgement(
                source=p.source,
                post_id=p.post_id,
                ticker=p.ticker,
                sentiment="neutral",
                confidence=0.5,
                one_line_summary=p.title[:120],
                is_stub=True,
            )
            for p in posts
        ]
