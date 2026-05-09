from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone

from market_sentiment.models import SocialPost, SocialSnapshot, SourceStatus
from market_sentiment.sources.base import SourcePayload


SOCIAL_PROVIDER_NAMES = ("reddit", "forum", "x")


class SocialProvider(ABC):
    name: str

    @abstractmethod
    def is_enabled(self) -> bool: ...

    @abstractmethod
    def fetch_posts(
        self,
        ticker: str,
        company_name: str,
        run_date: date,
        *,
        timeout_seconds: float | None = None,
    ) -> SourcePayload[list[SocialPost]]: ...


@dataclass(slots=True)
class SocialCollectionResult:
    snapshot: SocialSnapshot | None
    posts: list[SocialPost]
    statuses: list[SourceStatus]


def build_security_queries(ticker: str, company_name: str) -> list[str]:
    company_base = re.split(
        r",|\(|\binc\b|\bcorp\b|\bcorporation\b|\bltd\b|\bplc\b",
        company_name,
        flags=re.IGNORECASE,
    )[0]
    queries = [ticker.upper()]
    normalized = company_base.strip().strip(".")
    if normalized and normalized.lower() != ticker.lower():
        queries.append(normalized)
    unique_queries: list[str] = []
    for query in queries:
        if query and query not in unique_queries:
            unique_queries.append(query)
    return unique_queries


def matches_security_text(*, title: str, body: str, ticker: str, company_name: str) -> bool:
    text = f"{title}\n{body}"
    lower_text = text.lower()
    for query in build_security_queries(ticker, company_name)[1:]:
        normalized_query = query.strip().strip(".").lower()
        if normalized_query and re.search(
            rf"(?<![A-Za-z0-9]){re.escape(normalized_query)}(?![A-Za-z0-9])",
            lower_text,
            flags=re.IGNORECASE,
        ):
            return True
    if re.search(rf"\${re.escape(ticker)}(?![A-Za-z0-9])", text, flags=re.IGNORECASE):
        return True
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])", text, flags=re.IGNORECASE))


def author_hash(author: str) -> str | None:
    if not author:
        return None
    return hashlib.sha1(author.encode("utf-8")).hexdigest()


def dedupe_social_posts(posts: list[SocialPost]) -> list[SocialPost]:
    seen: dict[tuple[str, str], SocialPost] = {}
    for post in sorted(posts, key=lambda item: (_to_utc(item.created_at), item.engagement_score), reverse=True):
        text_key = re.sub(r"\s+", " ", f"{post.title}\n{post.body}".strip().lower()).strip()
        dedupe_key = (post.source, text_key[:240])
        if dedupe_key not in seen:
            seen[dedupe_key] = post
    return list(seen.values())


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
