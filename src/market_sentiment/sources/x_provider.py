from __future__ import annotations

import asyncio
import importlib
import inspect
from pathlib import Path
import threading
from datetime import date, datetime, timezone
from typing import Any

from market_sentiment.config import SocialConfig
from market_sentiment.models import SocialPost, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.social_base import SocialProvider, author_hash, build_security_queries, matches_security_text
from market_sentiment.storage import Storage


X_PROVIDER_URLS = {
    "twscrape": "https://github.com/vladkens/twscrape",
    "twikit": "https://github.com/d60/twikit",
}
TWIKIT_PAGE_SIZE = 20


class XClient(SocialProvider):
    name = "x"

    def __init__(self, config: SocialConfig, storage: Storage | None = None) -> None:
        self.config = config
        self.storage = storage

    def is_enabled(self) -> bool:
        return self.config.enabled and self.config.x.enabled

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
                status=SourceStatus(source=self.name, success=True, message="x provider disabled"),
            )

        limit = self._post_limit()
        if limit <= 0:
            provider_name = _provider_candidates(self.config.x.provider)[0]
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source=self.name,
                    success=True,
                    message="x provider enabled but max_posts resolved to 0",
                    source_url=X_PROVIDER_URLS.get(provider_name),
                ),
            )

        last_payload: SourcePayload[list[SocialPost]] | None = None
        first_successful_empty_payload: SourcePayload[list[SocialPost]] | None = None
        for provider_name in _provider_candidates(self.config.x.provider):
            if provider_name == "twscrape":
                payload = self._fetch_with_twscrape(
                    ticker=ticker,
                    company_name=company_name,
                    run_date=run_date,
                    limit=limit,
                    timeout_seconds=timeout_seconds,
                )
            elif provider_name == "twikit":
                payload = self._fetch_with_twikit(
                    ticker=ticker,
                    company_name=company_name,
                    run_date=run_date,
                    limit=limit,
                    timeout_seconds=timeout_seconds,
                )
            else:
                payload = self._unavailable(f"unsupported x provider: {provider_name}", provider_name)
            last_payload = payload
            if payload.data:
                return payload
            if payload.status.success and first_successful_empty_payload is None:
                first_successful_empty_payload = payload
                continue

        if first_successful_empty_payload is not None:
            return first_successful_empty_payload
        if last_payload is not None:
            return last_payload
        return self._unavailable(f"unsupported x provider: {self.config.x.provider}", self.config.x.provider.lower())

    def _fetch_with_twscrape(
        self,
        *,
        ticker: str,
        company_name: str,
        run_date: date,
        limit: int,
        timeout_seconds: float | None,
    ) -> SourcePayload[list[SocialPost]]:
        try:
            twscrape = importlib.import_module("twscrape")
        except Exception as exc:
            return self._unavailable(f"twscrape not installed: {exc}", "twscrape")

        async def _collect() -> tuple[list[SocialPost], list[dict[str, Any]], int]:
            api = _build_twscrape_api(twscrape, self.config)
            await _ensure_twscrape_accounts(api, self.config)
            query = _x_query(ticker, company_name)
            posts: list[SocialPost] = []
            raw_items: list[dict[str, Any]] = []
            dropped = 0
            seen_post_ids: set[str] = set()
            async for tweet in api.search(query, limit=limit):
                text = getattr(tweet, "rawContent", "") or getattr(tweet, "text", "")
                raw_items.append(_serialize_tweet(tweet, provider="twscrape"))
                if not text or not matches_security_text(title=text, body="", ticker=ticker, company_name=company_name):
                    dropped += 1
                    continue
                created_at = getattr(tweet, "date", None) or datetime.now(timezone.utc)
                user = getattr(tweet, "user", None)
                username = getattr(user, "username", None) or getattr(user, "displayname", None)
                post_id = str(getattr(tweet, "id", ""))
                if post_id and post_id in seen_post_ids:
                    continue
                if post_id:
                    seen_post_ids.add(post_id)
                posts.append(
                    SocialPost(
                        ticker=ticker,
                        source=self.name,
                        community="x",
                        post_id=post_id or f"x-{len(posts)}",
                        created_at=_normalize_dt(created_at),
                        title=text,
                        body="",
                        url=f"https://x.com/{username}/status/{post_id}" if username and post_id else "https://x.com",
                        author_handle=username,
                        author_id_hash=author_hash(username or ""),
                        engagement_score=float(
                            (getattr(tweet, "likeCount", 0) or 0)
                            + (getattr(tweet, "retweetCount", 0) or 0)
                            + (getattr(tweet, "replyCount", 0) or 0)
                        ),
                        comment_count=int(getattr(tweet, "replyCount", 0) or 0),
                        like_count=int(getattr(tweet, "likeCount", 0) or 0),
                        repost_count=int(getattr(tweet, "retweetCount", 0) or 0),
                        language=getattr(tweet, "lang", None) or "en",
                        matched_text=True,
                        source_query=query,
                        source_url=X_PROVIDER_URLS["twscrape"],
                        raw_payload_path=None,
                        ingested_at=datetime.now(timezone.utc),
                    )
                )
                if len(posts) >= limit:
                    break
            return posts, raw_items, dropped

        try:
            posts, raw_items, dropped = _run_async(_collect(), timeout_seconds=timeout_seconds)
        except Exception as exc:
            return self._unavailable(f"twscrape fetch failed: {exc}", "twscrape")
        raw_path = self._write_raw_payload(
            run_date=run_date,
            ticker=ticker,
            provider_name="twscrape",
            query=_x_query(ticker, company_name),
            items=raw_items,
        )
        posts = _attach_raw_path(posts, raw_path)
        return SourcePayload(
            data=posts,
            status=SourceStatus(
                source=self.name,
                success=True,
                partial=dropped > 0,
                message="x ok via twscrape" if dropped == 0 else f"x ok via twscrape; filtered {dropped} unmatched tweets",
                payload_path=raw_path,
                source_url=X_PROVIDER_URLS["twscrape"],
            ),
        )

    def _fetch_with_twikit(
        self,
        *,
        ticker: str,
        company_name: str,
        run_date: date,
        limit: int,
        timeout_seconds: float | None,
    ) -> SourcePayload[list[SocialPost]]:
        try:
            twikit = importlib.import_module("twikit")
        except Exception as exc:
            return self._unavailable(f"twikit not installed: {exc}", "twikit")

        async def _collect() -> tuple[list[SocialPost], list[dict[str, Any]], int]:
            client = _build_twikit_client(twikit, self.config)
            query = _x_query(ticker, company_name)
            session_loaded = False
            if self.config.x.cookies_path:
                session_loaded = await _load_twikit_cookies(client, self.config.x.cookies_path)

            if not session_loaded:
                await _login_twikit(client, self.config)

            posts: list[SocialPost] = []
            raw_items: list[dict[str, Any]] = []
            dropped = 0
            seen_post_ids: set[str] = set()
            try:
                result = await client.search_tweet(
                    query,
                    product=self.config.x.search_product,
                    count=min(limit, TWIKIT_PAGE_SIZE),
                )
            except Exception:
                if not session_loaded:
                    raise
                await _login_twikit(client, self.config)
                result = await client.search_tweet(
                    query,
                    product=self.config.x.search_product,
                    count=min(limit, TWIKIT_PAGE_SIZE),
                )
            while result is not None and len(posts) < limit:
                for tweet in result:
                    text = getattr(tweet, "text", "") or getattr(tweet, "full_text", "")
                    raw_items.append(_serialize_tweet(tweet, provider="twikit"))
                    if not text or not matches_security_text(title=text, body="", ticker=ticker, company_name=company_name):
                        dropped += 1
                        continue
                    user = getattr(tweet, "user", None)
                    username = getattr(user, "screen_name", None) or getattr(user, "name", None)
                    post_id = str(getattr(tweet, "id", ""))
                    if post_id and post_id in seen_post_ids:
                        continue
                    if post_id:
                        seen_post_ids.add(post_id)
                    posts.append(
                        SocialPost(
                            ticker=ticker,
                            source=self.name,
                            community="x",
                            post_id=post_id or f"x-{len(posts)}",
                            created_at=_normalize_dt(getattr(tweet, "created_at_datetime", None) or datetime.now(timezone.utc)),
                            title=text,
                            body="",
                            url=f"https://x.com/{username}/status/{post_id}" if username and post_id else "https://x.com",
                            author_handle=username,
                            author_id_hash=author_hash(username or ""),
                            engagement_score=float(
                                (getattr(tweet, "favorite_count", 0) or 0)
                                + (getattr(tweet, "retweet_count", 0) or 0)
                                + (getattr(tweet, "reply_count", 0) or 0)
                            ),
                            comment_count=int(getattr(tweet, "reply_count", 0) or 0),
                            like_count=int(getattr(tweet, "favorite_count", 0) or 0),
                            repost_count=int(getattr(tweet, "retweet_count", 0) or 0),
                            language=getattr(tweet, "lang", None) or "en",
                            matched_text=True,
                            source_query=query,
                            source_url=X_PROVIDER_URLS["twikit"],
                            raw_payload_path=None,
                            ingested_at=datetime.now(timezone.utc),
                        )
                    )
                    if len(posts) >= limit:
                        break
                if len(posts) >= limit:
                    break
                next_page = getattr(result, "next", None)
                if not callable(next_page):
                    break
                result = await next_page()
            return posts, raw_items, dropped

        try:
            posts, raw_items, dropped = _run_async(_collect(), timeout_seconds=timeout_seconds)
        except Exception as exc:
            return self._unavailable(f"twikit fetch failed: {exc}", "twikit")
        raw_path = self._write_raw_payload(
            run_date=run_date,
            ticker=ticker,
            provider_name="twikit",
            query=_x_query(ticker, company_name),
            items=raw_items,
        )
        posts = _attach_raw_path(posts, raw_path)
        return SourcePayload(
            data=posts,
            status=SourceStatus(
                source=self.name,
                success=True,
                partial=dropped > 0,
                message="x ok via twikit" if dropped == 0 else f"x ok via twikit; filtered {dropped} unmatched tweets",
                payload_path=raw_path,
                source_url=X_PROVIDER_URLS["twikit"],
            ),
        )

    def _post_limit(self) -> int:
        provider_limit = max(0, self.config.x.max_posts)
        service_limit = max(0, self.config.max_posts_per_source)
        if provider_limit == 0 or service_limit == 0:
            return 0
        return min(provider_limit, service_limit)

    def _write_raw_payload(
        self,
        *,
        run_date: date,
        ticker: str,
        provider_name: str,
        query: str,
        items: list[dict[str, Any]],
    ) -> str | None:
        if self.storage is None:
            return None
        path = self.storage.write_raw_json(
            run_date,
            "x",
            f"{ticker.lower()}_{provider_name}_{_safe_stem(query)}",
            {"provider": provider_name, "query": query, "items": items},
        )
        return str(path)

    def _unavailable(self, message: str, provider_name: str) -> SourcePayload[list[SocialPost]]:
        return SourcePayload(
            data=[],
            status=SourceStatus(
                source=self.name,
                success=False,
                partial=True,
                message=message,
                source_url=X_PROVIDER_URLS.get(provider_name),
            ),
        )


def _build_twscrape_api(twscrape_module, config: SocialConfig):
    kwargs: dict[str, str] = {}
    if config.x.db_path:
        kwargs["db_path"] = config.x.db_path
    if config.x.proxy_url:
        kwargs["proxy"] = config.x.proxy_url

    api = None
    if kwargs:
        try:
            api = twscrape_module.API(**kwargs)
        except TypeError:
            api = None

    if api is None and config.x.db_path:
        try:
            api = twscrape_module.API(db_path=config.x.db_path)
        except TypeError:
            api = None

    if api is None:
        api = twscrape_module.API()

    if config.x.proxy_url and hasattr(api, "proxy"):
        api.proxy = config.x.proxy_url
    return api


def _build_twikit_client(twikit_module, config: SocialConfig):
    kwargs = {"language": "en-US"}
    if config.x.proxy_url:
        kwargs["proxy"] = config.x.proxy_url

    client = None
    try:
        client = twikit_module.Client(**kwargs)
    except TypeError:
        client = twikit_module.Client(language="en-US")

    if config.x.proxy_url and hasattr(client, "proxy"):
        client.proxy = config.x.proxy_url
    return client


async def _load_twikit_cookies(client, cookies_path: str) -> bool:
    load_cookies = getattr(client, "load_cookies", None)
    if not callable(load_cookies):
        return False
    try:
        await _maybe_await(load_cookies(cookies_path))
        return True
    except Exception:
        return False


async def _login_twikit(client, config: SocialConfig) -> None:
    if not config.x.username or not config.x.password:
        raise RuntimeError("twikit credentials not configured")
    await client.login(
        auth_info_1=config.x.username,
        auth_info_2=config.x.email,
        password=config.x.password,
        cookies_file=config.x.cookies_path,
    )
    if config.x.cookies_path:
        save_cookies = getattr(client, "save_cookies", None)
        if callable(save_cookies):
            try:
                await _maybe_await(save_cookies(config.x.cookies_path))
            except Exception:
                pass


def _x_query(ticker: str, company_name: str) -> str:
    terms = [f"${ticker.upper()}"]
    terms.extend(build_security_queries(ticker, company_name))
    unique_terms: list[str] = []
    for term in terms:
        if term not in unique_terms:
            unique_terms.append(term)
    return " OR ".join(f'"{term}"' for term in unique_terms)


def _provider_candidates(value: str) -> list[str]:
    candidates = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not candidates:
        return ["twscrape"]
    unique_candidates: list[str] = []
    for candidate in candidates:
        if candidate not in unique_candidates:
            unique_candidates.append(candidate)
    return unique_candidates


def _normalize_dt(value: object) -> datetime:
    if isinstance(value, str) and value.strip():
        try:
            return _normalize_dt(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _run_async(coro, *, timeout_seconds: float | None = None):
    wrapped = coro
    if timeout_seconds is not None and timeout_seconds > 0:
        wrapped = asyncio.wait_for(coro, timeout_seconds)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(wrapped)

    outcome: dict[str, Any] = {}

    def runner() -> None:
        try:
            outcome["value"] = asyncio.run(wrapped)
        except Exception as exc:  # pragma: no cover - exercised via caller behavior
            outcome["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout_seconds if timeout_seconds is not None and timeout_seconds > 0 else None)
    if thread.is_alive():
        raise TimeoutError(f"x async fetch timed out after {timeout_seconds:.2f}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


def _serialize_tweet(tweet: object, *, provider: str) -> dict[str, Any]:
    user = getattr(tweet, "user", None)
    return {
        "provider": provider,
        "id": str(getattr(tweet, "id", "") or ""),
        "text": getattr(tweet, "rawContent", "") or getattr(tweet, "text", "") or getattr(tweet, "full_text", ""),
        "created_at": _normalize_dt(
            getattr(tweet, "date", None) or getattr(tweet, "created_at_datetime", None) or datetime.now(timezone.utc)
        ).isoformat(),
        "username": getattr(user, "username", None) or getattr(user, "screen_name", None) or getattr(user, "name", None),
        "like_count": int(getattr(tweet, "likeCount", None) or getattr(tweet, "favorite_count", 0) or 0),
        "reply_count": int(getattr(tweet, "replyCount", None) or getattr(tweet, "reply_count", 0) or 0),
        "repost_count": int(getattr(tweet, "retweetCount", None) or getattr(tweet, "retweet_count", 0) or 0),
        "lang": getattr(tweet, "lang", None),
    }


def _attach_raw_path(posts: list[SocialPost], raw_path: str | None) -> list[SocialPost]:
    if raw_path is None:
        return posts
    for post in posts:
        post.raw_payload_path = raw_path
    return posts


def _safe_stem(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_") or "query"


async def _ensure_twscrape_accounts(api: object, config: SocialConfig) -> None:
    accounts_file = config.x.accounts_file
    if not accounts_file:
        return
    if not config.x.db_path:
        raise RuntimeError("twscrape accounts bootstrap requires X_DB_PATH")
    db_path = Path(config.x.db_path)
    if db_path.exists() and db_path.stat().st_size > 0:
        return
    pool = getattr(api, "pool", None)
    add_account = getattr(pool, "add_account", None)
    login_all = getattr(pool, "login_all", None)
    if not callable(add_account) or not callable(login_all):
        raise RuntimeError("twscrape API pool is missing add_account/login_all")

    accounts = _load_twscrape_accounts(accounts_file, config.x.accounts_line_format)
    if not accounts:
        raise RuntimeError("twscrape accounts file is empty")
    for account in accounts:
        await _invoke_twscrape_add_account(add_account, account)
    await _maybe_await(login_all())


def _load_twscrape_accounts(accounts_file: str, line_format: str) -> list[dict[str, str | None]]:
    field_order = [field.strip().lower() for field in line_format.split(":") if field.strip()]
    supported_fields = {"username", "password", "email", "email_password", "phone", "cookies"}
    normalized_order = [field for field in field_order if field in supported_fields]
    if not normalized_order:
        normalized_order = ["username", "password", "email", "email_password"]

    accounts: list[dict[str, str | None]] = []
    for raw_line in Path(accounts_file).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        values = line.split(":")
        if len(values) < len(normalized_order):
            values.extend([""] * (len(normalized_order) - len(values)))
        account = {
            field: _normalize_account_value(values[index] if index < len(values) else "")
            for index, field in enumerate(normalized_order)
        }
        accounts.append(account)
    return accounts


async def _invoke_twscrape_add_account(add_account, account: dict[str, str | None]) -> None:
    signature = inspect.signature(add_account)
    parameters = [parameter for parameter in signature.parameters.values() if parameter.name != "self"]
    if any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        args = [
            account.get("username"),
            account.get("password"),
            account.get("email"),
            account.get("email_password"),
            account.get("phone"),
            account.get("cookies"),
        ]
        while args and args[-1] is None:
            args.pop()
        await _maybe_await(add_account(*args))
        return

    kwargs = {
        parameter.name: account.get(parameter.name)
        for parameter in parameters
        if parameter.name in account
    }
    await _maybe_await(add_account(**kwargs))


def _normalize_account_value(value: str) -> str | None:
    normalized = value.strip()
    if not normalized or normalized == "_":
        return None
    return normalized
