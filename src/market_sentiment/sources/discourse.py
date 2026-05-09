from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from market_sentiment.config import SocialConfig
from market_sentiment.http import HttpClient
from market_sentiment.models import SocialPost, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.social_base import SocialProvider, author_hash, build_security_queries, matches_security_text
from market_sentiment.storage import Storage


class DiscourseClient(SocialProvider):
    name = "forum"

    def __init__(self, http: HttpClient, storage: Storage, config: SocialConfig) -> None:
        self.http = http
        self.storage = storage
        self.config = config

    def is_enabled(self) -> bool:
        return self.config.enabled and self.config.forum.enabled and bool(self.config.forum.base_urls)

    def fetch_posts(
        self,
        ticker: str,
        company_name: str,
        run_date: date,
        *,
        timeout_seconds: float | None = None,
    ) -> SourcePayload[list[SocialPost]]:
        if not self.is_enabled():
            return SourcePayload(
                data=[],
                status=SourceStatus(source=self.name, success=True, message="forum social provider disabled"),
            )

        posts_by_key: dict[tuple[str, str], SocialPost] = {}
        failures: list[str] = []
        successes = 0
        per_forum_limit = max(0, min(self.config.forum.max_posts_per_forum, self.config.max_posts_per_source))
        headers = {}
        if self.config.forum.api_key:
            headers["Api-Key"] = self.config.forum.api_key
        if self.config.forum.api_username:
            headers["Api-Username"] = self.config.forum.api_username

        for base_url in self.config.forum.base_urls:
            if per_forum_limit == 0:
                break
            search_url = f"{_normalize_base_url(base_url)}/search.json"
            forum_posts = 0
            for query in build_security_queries(ticker, company_name):
                if forum_posts >= per_forum_limit:
                    break
                params = {"q": query}
                try:
                    response = _http_get(
                        self.http,
                        search_url,
                        params=params,
                        headers=headers or None,
                        timeout_seconds=timeout_seconds,
                    )
                    payload = response.json()
                    raw_path = self.storage.write_raw_json(
                        run_date,
                        "forum",
                        f"{ticker.lower()}_{_site_stem(base_url)}_{_safe_stem(query)}",
                        payload,
                    )
                    parsed_posts = _parse_discourse_payload(
                        payload=payload,
                        ticker=ticker,
                        company_name=company_name,
                        base_url=base_url,
                        raw_path=str(raw_path),
                        source_query=query,
                    )
                    for post in sorted(parsed_posts, key=lambda item: item.created_at, reverse=True):
                        key = (post.source, post.post_id)
                        if key in posts_by_key:
                            continue
                        posts_by_key[key] = post
                        forum_posts += 1
                        if forum_posts >= per_forum_limit:
                            break
                    successes += 1
                except Exception as exc:
                    failures.append(f"{base_url}/{query}: {exc}")

        success = successes > 0
        partial = bool(failures)
        message = "forum ok"
        if failures and success:
            message = f"forum partial success; failed sites: {'; '.join(failures[:3])}"
        elif failures and not success:
            message = f"forum fetch failed: {'; '.join(failures[:3])}"
        return SourcePayload(
            data=sorted(posts_by_key.values(), key=lambda item: item.created_at, reverse=True),
            status=SourceStatus(
                source=self.name,
                success=success,
                partial=partial,
                message=message,
                source_url="https://meta.discourse.org/t/discourse-rest-api-documentation/22706",
            ),
        )


def _parse_discourse_payload(
    *,
    payload: dict,
    ticker: str,
    company_name: str,
    base_url: str,
    raw_path: str,
    source_query: str,
) -> list[SocialPost]:
    posts: list[SocialPost] = []
    seen_ids: set[str] = set()
    for topic in payload.get("topics", []) or []:
        topic_id = str(topic.get("id") or "")
        title = str(topic.get("title") or "")
        blurb = str(topic.get("blurb") or "")
        if not matches_security_text(title=title, body=blurb, ticker=ticker, company_name=company_name):
            continue
        if topic_id in seen_ids:
            continue
        seen_ids.add(topic_id)
        slug = str(topic.get("slug") or topic_id)
        created_at = _parse_datetime(topic.get("created_at"))
        if created_at is None:
            continue
        posts.append(
            SocialPost(
                ticker=ticker,
                source="forum",
                community=_site_stem(base_url),
                post_id=topic_id or hashlib.sha1((title + blurb).encode("utf-8")).hexdigest(),
                created_at=created_at,
                title=title,
                body=blurb,
                url=f"{_normalize_base_url(base_url)}/t/{slug}/{topic_id}" if topic_id else _normalize_base_url(base_url),
                author_handle=str(topic.get("username") or "") or None,
                author_id_hash=author_hash(str(topic.get("username") or "")),
                engagement_score=float((topic.get("posts_count") or 0) + (topic.get("views") or 0) / 100.0),
                comment_count=int(topic.get("posts_count") or 0),
                language="en",
                matched_text=True,
                source_query=source_query,
                source_url=f"{_normalize_base_url(base_url)}/search.json",
                raw_payload_path=raw_path,
                ingested_at=datetime.now(timezone.utc),
            )
        )

    for post in payload.get("posts", []) or []:
        post_id = str(post.get("id") or "")
        title = str(post.get("topic_title") or "")
        cooked = str(post.get("blurb") or post.get("cooked") or post.get("excerpt") or "")
        if not matches_security_text(title=title, body=cooked, ticker=ticker, company_name=company_name):
            continue
        if post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        created_at = _parse_datetime(post.get("created_at"))
        if created_at is None:
            continue
        topic_id = str(post.get("topic_id") or post_id)
        posts.append(
            SocialPost(
                ticker=ticker,
                source="forum",
                community=_site_stem(base_url),
                post_id=post_id or hashlib.sha1((title + cooked).encode("utf-8")).hexdigest(),
                created_at=created_at,
                title=title,
                body=cooked,
                url=f"{_normalize_base_url(base_url)}/t/{topic_id}" if topic_id else _normalize_base_url(base_url),
                author_handle=str(post.get("username") or "") or None,
                author_id_hash=author_hash(str(post.get("username") or "")),
                engagement_score=float(post.get("score") or post.get("reads") or 0),
                comment_count=None,
                language="en",
                matched_text=True,
                source_query=source_query,
                source_url=f"{_normalize_base_url(base_url)}/search.json",
                raw_payload_path=raw_path,
                ingested_at=datetime.now(timezone.utc),
            )
        )
    return posts


def _normalize_base_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if not parts.scheme:
        return f"https://{base_url.rstrip('/')}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _site_stem(base_url: str) -> str:
    normalized = _normalize_base_url(base_url)
    return urlsplit(normalized).netloc.replace(".", "_")


def _safe_stem(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_") or "query"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _http_get(
    http: HttpClient,
    url: str,
    *,
    params: dict[str, str],
    headers: dict[str, str] | None,
    timeout_seconds: float | None,
):
    try:
        return http.get(url, params=params, headers=headers, timeout_seconds=timeout_seconds)
    except TypeError:
        return http.get(url, params=params, headers=headers)
