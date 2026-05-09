from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone

from market_sentiment.config import SocialConfig
from market_sentiment.http import HttpClient
from market_sentiment.models import SocialPost, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.social_base import SocialProvider, author_hash, build_security_queries, matches_security_text
from market_sentiment.storage import Storage


class RedditClient(SocialProvider):
    name = "reddit"

    def __init__(self, http: HttpClient, storage: Storage, config: SocialConfig) -> None:
        self.http = http
        self.storage = storage
        self.config = config

    def is_enabled(self) -> bool:
        reddit = self.config.reddit
        return self.config.enabled and reddit.enabled

    def fetch_posts(
        self,
        ticker: str,
        company_name: str,
        run_date: date,
        *,
        timeout_seconds: float | None = None,
    ) -> SourcePayload[list[SocialPost]]:
        reddit = self.config.reddit
        if not self.is_enabled():
            return SourcePayload(
                data=[],
                status=SourceStatus(source="reddit", success=True, message="reddit social provider disabled"),
            )

        posts: list[SocialPost] = []
        failures: list[str] = []
        successes = 0
        seen_post_ids: set[str] = set()
        queries = build_security_queries(ticker, company_name)
        for subreddit in reddit.subreddits:
            for query in queries:
                params = {
                    "limit": str(reddit.max_posts_per_subreddit),
                    "restrict_sr": "1",
                    "sort": "new",
                    "q": query,
                    "t": "month",
                }
                url = f"https://www.reddit.com/r/{subreddit}/search.json"
                try:
                    response = _http_get(
                        self.http,
                        url,
                        params=params,
                        headers={"User-Agent": reddit.user_agent},
                        timeout_seconds=timeout_seconds,
                    )
                    payload = response.json()
                    raw_path = self.storage.write_raw_json(
                        run_date,
                        "reddit",
                        f"{ticker.lower()}_{subreddit.lower()}_{_safe_stem(query)}",
                        payload,
                    )
                    for child in payload.get("data", {}).get("children", []):
                        data = child.get("data", {})
                        try:
                            post = self._to_post(
                                ticker=ticker,
                                company_name=company_name,
                                subreddit=subreddit,
                                data=data,
                                raw_path=raw_path,
                                source_query=query,
                            )
                        except Exception as exc:
                            failures.append(f"{subreddit}/{query}/item: {exc}")
                            continue
                        if post is not None and post.post_id not in seen_post_ids:
                            seen_post_ids.add(post.post_id)
                            posts.append(post)
                    successes += 1
                except Exception as exc:
                    failures.append(f"{subreddit}/{query}: {exc}")

        partial = bool(failures)
        success = successes > 0
        message = "reddit ok"
        if failures and successes:
            message = f"reddit partial success; failed sources: {'; '.join(failures[:3])}"
        elif failures and not successes:
            message = f"reddit fetch failed: {'; '.join(failures[:3])}"

        return SourcePayload(
            data=posts,
            status=SourceStatus(
                source="reddit",
                success=success,
                partial=partial,
                message=message,
                source_url="https://www.reddit.com/dev/api/",
            ),
        )

    def _to_post(
        self,
        *,
        ticker: str,
        company_name: str,
        subreddit: str,
        data: dict,
        raw_path,
        source_query: str | None = None,
    ) -> SocialPost | None:
        title = str(data.get("title") or "")
        body = str(data.get("selftext") or "")
        if not matches_security_text(title=title, body=body, ticker=ticker, company_name=company_name):
            return None
        created_at = _parse_created_at(data.get("created_utc"))
        if created_at is None:
            return None
        permalink = str(data.get("permalink") or "")
        return SocialPost(
            ticker=ticker,
            source="reddit",
            community=subreddit,
            post_id=str(data.get("id") or permalink or hashlib.sha1((title + body).encode("utf-8")).hexdigest()),
            created_at=created_at,
            title=title,
            body=body,
            url=f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink or "https://www.reddit.com",
            author_handle=str(data.get("author") or "") or None,
            author_id_hash=author_hash(str(data.get("author") or "")),
            engagement_score=float((data.get("score") or 0) + (data.get("num_comments") or 0)),
            comment_count=int(data.get("num_comments") or 0),
            like_count=int(data.get("ups") or 0),
            repost_count=0,
            language="en",
            is_repost=bool(data.get("crosspost_parent")),
            matched_text=True,
            source_query=source_query or ticker,
            source_url="https://www.reddit.com/dev/api/",
            raw_payload_path=str(raw_path),
            ingested_at=datetime.now(timezone.utc),
        )


def _matches_security_text(*, title: str, body: str, ticker: str, company_name: str) -> bool:
    return matches_security_text(title=title, body=body, ticker=ticker, company_name=company_name)


def _author_hash(author: str) -> str | None:
    return author_hash(author)


def _safe_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").lower() or "query"


def _parse_created_at(value: object) -> datetime | None:
    try:
        created_ts = float(value)
    except (TypeError, ValueError):
        return None
    if created_ts <= 0:
        return None
    return datetime.fromtimestamp(created_ts, tz=timezone.utc)


def _http_get(http: HttpClient, url: str, *, params: dict[str, str], headers: dict[str, str], timeout_seconds: float | None):
    try:
        return http.get(url, params=params, headers=headers, timeout_seconds=timeout_seconds)
    except TypeError:
        return http.get(url, params=params, headers=headers)
