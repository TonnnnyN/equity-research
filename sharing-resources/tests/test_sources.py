from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from unittest import TestCase
from unittest.mock import patch

from market_sentiment.config import OptionsConfig, load_config
from market_sentiment.http import HttpClient, redact_url
from market_sentiment.models import PriceBar, SourceStatus
from market_sentiment.pipeline import DailyPipeline
from market_sentiment.sources.eia import EiaClient
from market_sentiment.sources.options_alpha_vantage import AlphaVantageOptionsClient
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.discourse import DiscourseClient
from market_sentiment.sources.reddit import RedditClient
from market_sentiment.sources.sec import SecClient, _extract_latest_pair
from market_sentiment.sources.x import XClient
from market_sentiment.storage import Storage
from market_sentiment.sources.social_base import matches_security_text


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "watchlist.toml"


class FakeResponse:
    def __init__(self, url: str, payload):
        self.url = url
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, payload: dict):
        self._payload = payload
        self.last_url = ""
        self.last_params = None

    def get(self, url: str, params=None, headers=None):
        self.last_url = url
        self.last_params = params
        return FakeResponse(url, self._payload)


class RoutingFakeHttpClient:
    def __init__(self, payloads: dict[str, dict]):
        self.payloads = payloads

    def get(self, url: str, params=None, headers=None):
        for prefix, payload in self.payloads.items():
            if url.startswith(prefix):
                return FakeResponse(url, payload)
        raise AssertionError(f"Unexpected URL: {url}")


class FailingTickerMapHttpClient:
    def get(self, url: str, params=None, headers=None):
        if url == SecClient.ticker_map_url:
            raise RuntimeError("HTTP 503 from SEC ticker map")
        return FakeResponse(url, {})


class MixedResultHttpClient:
    def __init__(self, success_payload: dict, failing_urls: set[str]) -> None:
        self.success_payload = success_payload
        self.failing_urls = failing_urls

    def get(self, url: str, params=None, headers=None):
        query = str((params or {}).get("q") or "")
        key = f"{url}|{query}"
        if key in self.failing_urls:
            raise RuntimeError("upstream unavailable")
        return FakeResponse(url, self.success_payload)


class FakeTwscrapePool:
    def __init__(self) -> None:
        self.add_account_calls: list[tuple[tuple, dict]] = []
        self.login_all_calls = 0

    async def add_account(self, *args, **kwargs) -> None:
        self.add_account_calls.append((args, kwargs))

    async def login_all(self) -> None:
        self.login_all_calls += 1


class FakeTwscrapeApi:
    last_instance = None

    def __init__(self, db_path=None, proxy=None) -> None:
        self.db_path = db_path
        self.proxy = proxy
        self.pool = FakeTwscrapePool()
        self.search_calls: list[tuple[str, int]] = []
        FakeTwscrapeApi.last_instance = self

    async def search(self, query, limit):
        self.search_calls.append((query, limit))
        tweets = [
            SimpleNamespace(
                id="1",
                rawContent="$MSFT demand improving and backlog looks stronger",
                date=datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc),
                user=SimpleNamespace(username="alpha"),
                likeCount=12,
                retweetCount=3,
                replyCount=1,
                lang="en",
            ),
            SimpleNamespace(
                id="2",
                rawContent="MSFT margins improving after the selloff",
                date="2026-03-26T11:00:00Z",
                user=SimpleNamespace(username="beta"),
                likeCount=9,
                retweetCount=2,
                replyCount=1,
                lang="en",
            ),
            SimpleNamespace(
                id="3",
                rawContent="Completely unrelated post",
                date=datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc),
                user=SimpleNamespace(username="gamma"),
                likeCount=1,
                retweetCount=0,
                replyCount=0,
                lang="en",
            ),
        ]
        for tweet in tweets[:limit]:
            yield tweet


class FakeTwscrapeModule:
    API = FakeTwscrapeApi


class SourceTests(TestCase):
    def test_redact_url_masks_known_api_key_params(self) -> None:
        redacted = redact_url("https://example.com/data?api_key=secret&series_id=DGS10&apikey=also-secret")

        self.assertIn("api_key=REDACTED", redacted)
        self.assertIn("apikey=REDACTED", redacted)
        self.assertIn("series_id=DGS10", redacted)
        self.assertNotIn("secret", redacted)

    def test_http_client_redacts_api_keys_in_network_errors(self) -> None:
        client = HttpClient("test-agent")

        with patch("market_sentiment.http.urlopen", side_effect=URLError("boom")):
            with self.assertRaisesRegex(RuntimeError, "api_key=REDACTED") as raised:
                client.get("https://example.com/data", params={"api_key": "secret", "series_id": "DGS10"})

        self.assertNotIn("secret", str(raised.exception))

    def test_load_config_maps_legacy_eia_route_to_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "watchlist.toml"
            config_path.write_text(
                """
[project]
timezone = "Asia/Hong_Kong"
raw_data_dir = "data/raw"
db_path = "data/state/market_sentiment.sqlite3"

[triggers.compute]
drawdown_10d = -0.10
drawdown_20d = -0.20
relative_20d = -0.05

[macro]
fred_series = ["DGS10"]
eia_natural_gas_route = "/v2/natural-gas/pri/sum/data/"
""".strip(),
                encoding="utf-8",
            )

            config = load_config(str(config_path))

            self.assertEqual(config.eia_series, {"natural_gas": "/v2/natural-gas/pri/sum/data/"})

    def test_eia_supports_route_style_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["EIA_API_KEY"] = "demo"
            payload = {
                "response": {
                    "data": [
                        {"period": "2026-02", "value": "3.1"},
                        {"period": "2026-01", "value": "2.9"},
                    ]
                }
            }
            http = FakeHttpClient(payload)
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            client = EiaClient(http, storage)

            result = client.fetch_series("natural_gas", "/v2/natural-gas/pri/sum/data/", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 2)
            self.assertIn("/v2/natural-gas/pri/sum/data/", http.last_url)
            self.assertEqual(http.last_params["data[0]"], "value")

    def test_reddit_matches_bare_three_letter_ticker_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            pipeline_config = load_config(str(CONFIG_PATH))
            client = RedditClient(FakeHttpClient({}), storage, pipeline_config.social)

            post = client._to_post(
                ticker="AMD",
                company_name="Advanced Micro Devices, Inc.",
                subreddit="stocks",
                data={
                    "id": "abc123",
                    "created_utc": 1742995200,
                    "title": "AMD looks stronger after the selloff",
                    "selftext": "",
                    "permalink": "/r/stocks/comments/abc123/amd_looks_stronger_after_the_selloff/",
                    "author": "tester",
                    "score": 12,
                    "num_comments": 4,
                    "ups": 10,
                },
                raw_path=storage.raw_path(date(2026, 3, 26), "reddit", "amd_stocks"),
            )

            self.assertIsNotNone(post)
            self.assertEqual(post.ticker, "AMD")

    def test_social_matching_accepts_lowercase_ticker_and_short_company_name(self) -> None:
        self.assertTrue(
            matches_security_text(
                title="$amd demand improving after the selloff",
                body="",
                ticker="AMD",
                company_name="Advanced Micro Devices, Inc.",
            )
        )
        self.assertTrue(
            matches_security_text(
                title="Nvidia demand improving with stronger backlog",
                body="Discussion of margin recovery and cash flow.",
                ticker="NVDA",
                company_name="NVIDIA Corporation",
            )
        )

    def test_reddit_generates_fallback_post_id_when_payload_lacks_id_and_permalink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            pipeline_config = load_config(str(CONFIG_PATH))
            client = RedditClient(FakeHttpClient({}), storage, pipeline_config.social)

            post = client._to_post(
                ticker="AMD",
                company_name="Advanced Micro Devices, Inc.",
                subreddit="stocks",
                data={
                    "created_utc": 1742995200,
                    "title": "AMD looks stronger after the selloff",
                    "selftext": "Detailed take on margin recovery.",
                    "author": "tester",
                    "score": 12,
                    "num_comments": 4,
                    "ups": 10,
                },
                raw_path=storage.raw_path(date(2026, 3, 26), "reddit", "amd_stocks"),
            )

            self.assertIsNotNone(post)
            self.assertTrue(post.post_id)

    def test_reddit_matches_base_company_name_without_legal_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            pipeline_config = load_config(str(CONFIG_PATH))
            client = RedditClient(FakeHttpClient({}), storage, pipeline_config.social)

            post = client._to_post(
                ticker="MSFT",
                company_name="Microsoft Corporation",
                subreddit="stocks",
                data={
                    "id": "msft-base-name",
                    "created_utc": 1742995200,
                    "title": "Microsoft demand looks stronger after Azure checks",
                    "selftext": "Backlog and cash flow commentary remain constructive.",
                    "permalink": "/r/stocks/comments/msftbase/microsoft_demand/",
                    "author": "tester",
                    "score": 18,
                    "num_comments": 6,
                    "ups": 15,
                },
                raw_path=storage.raw_path(date(2026, 3, 26), "reddit", "msft_stocks"),
            )

            self.assertIsNotNone(post)
            self.assertEqual(post.ticker, "MSFT")

    def test_reddit_fetch_skips_malformed_posts_without_dropping_valid_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            pipeline_config = load_config(str(CONFIG_PATH))
            payload = {
                "data": {
                    "children": [
                        {
                            "data": {
                                "id": "bad-ts",
                                "created_utc": "not-a-timestamp",
                                "title": "MSFT noise with malformed timestamp",
                                "selftext": "",
                                "permalink": "/r/stocks/comments/bad/msft_noise/",
                                "author": "broken",
                            }
                        },
                        {
                            "data": {
                                "id": "good-post",
                                "created_utc": 1742995200,
                                "title": "Microsoft demand keeps improving",
                                "selftext": "Cash flow and backlog both look stronger.",
                                "permalink": "/r/stocks/comments/good/microsoft_demand/",
                                "author": "valid",
                                "score": 15,
                                "num_comments": 5,
                                "ups": 11,
                            }
                        },
                    ]
                }
            }
            client = RedditClient(FakeHttpClient(payload), storage, pipeline_config.social)

            result = client.fetch_posts("MSFT", "Microsoft Corporation", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 1)
            self.assertEqual(result.data[0].post_id, "good-post")

    def test_discourse_provider_parses_public_search_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            pipeline_config = load_config(str(CONFIG_PATH))
            pipeline_config.social.forum.enabled = True
            pipeline_config.social.forum.base_urls = ["https://forum.example.com"]
            payload = {
                "topics": [
                    {
                        "id": 42,
                        "slug": "amd-demand-is-improving",
                        "title": "AMD demand is improving after the selloff",
                        "blurb": "Longer form discussion on margin recovery.",
                        "username": "analyst",
                        "posts_count": 8,
                        "views": 1200,
                        "created_at": "2026-03-25T09:00:00Z",
                    }
                ]
            }
            client = DiscourseClient(FakeHttpClient(payload), storage, pipeline_config.social)

            result = client.fetch_posts("AMD", "Advanced Micro Devices, Inc.", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 1)
            self.assertEqual(result.data[0].source, "forum")

    def test_discourse_provider_skips_malformed_timestamps_instead_of_treating_them_as_now(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            pipeline_config = load_config(str(CONFIG_PATH))
            pipeline_config.social.forum.enabled = True
            pipeline_config.social.forum.base_urls = ["https://forum.example.com"]
            payload = {
                "topics": [
                    {
                        "id": 100,
                        "slug": "bad-timestamp",
                        "title": "MSFT demand is improving",
                        "blurb": "Malformed timestamp should not become fresh evidence.",
                        "username": "analyst",
                        "posts_count": 8,
                        "views": 1200,
                        "created_at": "not-a-real-datetime",
                    }
                ]
            }
            client = DiscourseClient(FakeHttpClient(payload), storage, pipeline_config.social)

            result = client.fetch_posts("MSFT", "Microsoft Corporation", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 0)

    def test_discourse_provider_caps_results_and_reports_partial_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            pipeline_config = load_config(str(CONFIG_PATH))
            pipeline_config.social.enabled = True
            pipeline_config.social.max_posts_per_source = 2
            pipeline_config.social.forum.enabled = True
            pipeline_config.social.forum.max_posts_per_forum = 3
            pipeline_config.social.forum.base_urls = ["https://forum.example.com", "https://forum2.example.com"]
            payload = {
                "topics": [
                    {
                        "id": 42,
                        "slug": "amd-demand-is-improving",
                        "title": "AMD demand is improving after the selloff",
                        "blurb": "Longer form discussion on margin recovery.",
                        "username": "analyst",
                        "posts_count": 8,
                        "views": 1200,
                        "created_at": "2026-03-25T09:00:00Z",
                    },
                    {
                        "id": 43,
                        "slug": "amd-cash-flow-recovers",
                        "title": "AMD cash flow recovers",
                        "blurb": "Discussion of cash generation and backlog strength.",
                        "username": "pm",
                        "posts_count": 6,
                        "views": 900,
                        "created_at": "bad-timestamp",
                    },
                    {
                        "id": 44,
                        "slug": "amd-third-hit",
                        "title": "AMD margins may stabilize",
                        "blurb": "Another post that should be trimmed by the cap.",
                        "username": "researcher",
                        "posts_count": 4,
                        "views": 500,
                        "created_at": "2026-03-24T09:00:00Z",
                    },
                ]
            }
            client = DiscourseClient(
                MixedResultHttpClient(
                    success_payload=payload,
                    failing_urls={
                        "https://forum2.example.com/search.json|AMD",
                        "https://forum2.example.com/search.json|Advanced Micro Devices",
                    },
                ),
                storage,
                pipeline_config.social,
            )

            result = client.fetch_posts("AMD", "Advanced Micro Devices, Inc.", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertEqual(len(result.data), 2)
            self.assertIn("failed sites", result.status.message)
            self.assertTrue(all(post.raw_payload_path for post in result.data))
            self.assertNotIn("AMD cash flow recovers", [post.title for post in result.data])

    def test_x_provider_gracefully_degrades_when_optional_dependency_missing(self) -> None:
        pipeline_config = load_config(str(CONFIG_PATH))
        pipeline_config.social.x.enabled = True
        pipeline_config.social.x.provider = "definitely-not-supported"
        client = XClient(pipeline_config.social)

        result = client.fetch_posts("MSFT", "Microsoft Corporation", date(2026, 3, 26))

        self.assertFalse(result.status.success)
        self.assertTrue(result.status.partial)
        self.assertIn("unsupported x provider", result.status.message)

    def test_x_provider_caps_results_and_persists_raw_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            pipeline_config = load_config(str(CONFIG_PATH))
            pipeline_config.social.enabled = True
            pipeline_config.social.max_posts_per_source = 5
            pipeline_config.social.x.enabled = True
            pipeline_config.social.x.provider = "twscrape"
            pipeline_config.social.x.max_posts = 5
            tweets = [
                SimpleNamespace(
                    id="1",
                    rawContent="$MSFT demand improving and backlog looks stronger",
                    date=datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc),
                    user=SimpleNamespace(username="alpha"),
                    likeCount=12,
                    retweetCount=3,
                    replyCount=1,
                    lang="en",
                ),
                SimpleNamespace(
                    id="2",
                    rawContent="MSFT margins improving after the selloff",
                    date="2026-03-26T11:00:00Z",
                    user=SimpleNamespace(username="beta"),
                    likeCount=9,
                    retweetCount=2,
                    replyCount=1,
                    lang="en",
                ),
                SimpleNamespace(
                    id="3",
                    rawContent="Completely unrelated post",
                    date=datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc),
                    user=SimpleNamespace(username="gamma"),
                    likeCount=1,
                    retweetCount=0,
                    replyCount=0,
                    lang="en",
                ),
            ]

            class FakeApi:
                async def search(self, query, limit):
                    for tweet in tweets[:limit]:
                        yield tweet

            fake_module = SimpleNamespace(API=lambda db_path=None: FakeApi())
            client = XClient(pipeline_config.social, storage=storage)

            with patch("market_sentiment.sources.x_provider.importlib.import_module", return_value=fake_module):
                result = client.fetch_posts("MSFT", "Microsoft Corporation", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertEqual(len(result.data), 2)
            self.assertTrue(result.status.payload_path)
            self.assertTrue(all(post.raw_payload_path for post in result.data))
            raw_files = list((Path(tmp) / "raw" / "2026-03-26" / "x").glob("*.json"))
            self.assertEqual(len(raw_files), 1)

    def test_x_provider_passes_db_path_and_proxy_to_twscrape_api(self) -> None:
        pipeline_config = load_config(str(CONFIG_PATH))
        pipeline_config.social.enabled = True
        pipeline_config.social.max_posts_per_source = 1
        pipeline_config.social.x.enabled = True
        pipeline_config.social.x.provider = "twscrape"
        pipeline_config.social.x.max_posts = 1
        pipeline_config.social.x.db_path = "/tmp/twscrape-accounts.db"
        pipeline_config.social.x.proxy_url = "socks5://127.0.0.1:1080"
        created_apis: list[object] = []

        class FakeApi:
            def __init__(self, db_path=None, proxy=None):
                self.db_path = db_path
                self.proxy = proxy
                created_apis.append(self)

            async def search(self, query, limit):
                if False:
                    yield None

        fake_module = SimpleNamespace(API=FakeApi)
        client = XClient(pipeline_config.social)

        with patch("market_sentiment.sources.x_provider.importlib.import_module", return_value=fake_module):
            result = client.fetch_posts("MSFT", "Microsoft Corporation", date(2026, 3, 26))

        self.assertTrue(result.status.success)
        self.assertEqual(len(created_apis), 1)
        self.assertEqual(created_apis[0].db_path, "/tmp/twscrape-accounts.db")
        self.assertEqual(created_apis[0].proxy, "socks5://127.0.0.1:1080")

    def test_x_provider_bootstraps_twscrape_accounts_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            accounts_file = Path(tmp) / "x_accounts.txt"
            accounts_file.write_text(
                "user1:pass1:mail1@example.com:mailpass1:_:cookies1.json\n"
                "user2:pass2:mail2@example.com:mailpass2:_:cookies2.json\n",
                encoding="utf-8",
            )
            pipeline_config = load_config(str(CONFIG_PATH))
            pipeline_config.social.enabled = True
            pipeline_config.social.max_posts_per_source = 1
            pipeline_config.social.x.enabled = True
            pipeline_config.social.x.provider = "twscrape"
            pipeline_config.social.x.max_posts = 1
            pipeline_config.social.x.db_path = str(Path(tmp) / "twscrape_accounts.db")
            pipeline_config.social.x.accounts_file = str(accounts_file)
            client = XClient(pipeline_config.social)

            with patch("market_sentiment.sources.x_provider.importlib.import_module", return_value=FakeTwscrapeModule):
                result = client.fetch_posts("MSFT", "Microsoft Corporation", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            api = FakeTwscrapeApi.last_instance
            self.assertIsNotNone(api)
            self.assertEqual(len(api.pool.add_account_calls), 2)
            self.assertEqual(api.pool.login_all_calls, 1)

    def test_x_provider_falls_back_to_next_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            pipeline_config = load_config(str(CONFIG_PATH))
            pipeline_config.social.enabled = True
            pipeline_config.social.max_posts_per_source = 3
            pipeline_config.social.x.enabled = True
            pipeline_config.social.x.provider = "twscrape,twikit"
            pipeline_config.social.x.max_posts = 3
            pipeline_config.social.x.username = "user"
            pipeline_config.social.x.password = "secret"

            tweet = SimpleNamespace(
                id="twikit-1",
                text="$MSFT demand improving",
                created_at_datetime=datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc),
                user=SimpleNamespace(screen_name="fallback-user"),
                favorite_count=7,
                retweet_count=2,
                reply_count=1,
                lang="en",
            )

            class FakeTwikitClient:
                async def login(self, **kwargs):
                    return None

                async def search_tweet(self, query, product, count):
                    return [tweet]

            def fake_import(name):
                if name == "twscrape":
                    raise ModuleNotFoundError("missing twscrape")
                if name == "twikit":
                    return SimpleNamespace(Client=lambda language="en-US": FakeTwikitClient())
                raise AssertionError(f"unexpected import: {name}")

            client = XClient(pipeline_config.social, storage=storage)

            with patch("market_sentiment.sources.x_provider.importlib.import_module", side_effect=fake_import):
                result = client.fetch_posts("MSFT", "Microsoft Corporation", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 1)
            self.assertIn("twikit", result.status.message)
            self.assertEqual(result.data[0].author_handle, "fallback-user")

    def test_x_provider_falls_back_when_first_backend_returns_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            pipeline_config = load_config(str(CONFIG_PATH))
            pipeline_config.social.enabled = True
            pipeline_config.social.max_posts_per_source = 3
            pipeline_config.social.x.enabled = True
            pipeline_config.social.x.provider = "twscrape,twikit"
            pipeline_config.social.x.max_posts = 3
            pipeline_config.social.x.username = "user"
            pipeline_config.social.x.password = "secret"

            tweet = SimpleNamespace(
                id="twikit-2",
                text="Microsoft margins improving after the selloff",
                created_at_datetime=datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc),
                user=SimpleNamespace(screen_name="fallback-user"),
                favorite_count=7,
                retweet_count=2,
                reply_count=1,
                lang="en",
            )

            class EmptyTwscrapeApi:
                async def search(self, query, limit):
                    if False:
                        yield None

            class FakeTwikitClient:
                async def login(self, **kwargs):
                    return None

                async def search_tweet(self, query, product, count):
                    return [tweet]

            def fake_import(name):
                if name == "twscrape":
                    return SimpleNamespace(API=lambda **kwargs: EmptyTwscrapeApi())
                if name == "twikit":
                    return SimpleNamespace(Client=lambda language="en-US": FakeTwikitClient())
                raise AssertionError(f"unexpected import: {name}")

            client = XClient(pipeline_config.social, storage=storage)

            with patch("market_sentiment.sources.x_provider.importlib.import_module", side_effect=fake_import):
                result = client.fetch_posts("MSFT", "Microsoft Corporation", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 1)
            self.assertIn("twikit", result.status.message)

    def test_x_provider_twikit_paginates_beyond_first_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            pipeline_config = load_config(str(CONFIG_PATH))
            pipeline_config.social.enabled = True
            pipeline_config.social.max_posts_per_source = 25
            pipeline_config.social.x.enabled = True
            pipeline_config.social.x.provider = "twikit"
            pipeline_config.social.x.max_posts = 25
            pipeline_config.social.x.username = "user"
            pipeline_config.social.x.password = "secret"

            def make_tweet(index: int):
                return SimpleNamespace(
                    id=f"tweet-{index}",
                    text=f"$MSFT improving demand {index}",
                    created_at_datetime=datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc),
                    user=SimpleNamespace(screen_name=f"user-{index}"),
                    favorite_count=5,
                    retweet_count=1,
                    reply_count=1,
                    lang="en",
                )

            class FakePage(list):
                def __init__(self, items, next_page=None):
                    super().__init__(items)
                    self._next_page = next_page

                async def next(self):
                    return self._next_page

            class FakeTwikitClient:
                async def login(self, **kwargs):
                    return None

                async def search_tweet(self, query, product, count):
                    self.count = count
                    second_page = FakePage([make_tweet(index) for index in range(21, 31)], None)
                    return FakePage([make_tweet(index) for index in range(1, 21)], second_page)

            fake_module = SimpleNamespace(Client=lambda language="en-US": FakeTwikitClient())
            client = XClient(pipeline_config.social, storage=storage)

            with patch("market_sentiment.sources.x_provider.importlib.import_module", return_value=fake_module):
                result = client.fetch_posts("MSFT", "Microsoft Corporation", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 25)
            self.assertTrue(result.status.payload_path)

    def test_x_provider_twikit_reuses_cookies_and_respects_proxy_and_search_product(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            cookies_path = Path(tmp) / "x_cookies.json"
            cookies_path.write_text("{}", encoding="utf-8")
            pipeline_config = load_config(str(CONFIG_PATH))
            pipeline_config.social.enabled = True
            pipeline_config.social.max_posts_per_source = 5
            pipeline_config.social.x.enabled = True
            pipeline_config.social.x.provider = "twikit"
            pipeline_config.social.x.max_posts = 5
            pipeline_config.social.x.search_product = "Top"
            pipeline_config.social.x.proxy_url = "http://proxy.local:8080"
            pipeline_config.social.x.cookies_path = str(cookies_path)
            pipeline_config.social.x.username = "user"
            pipeline_config.social.x.password = "secret"
            created_clients: list[object] = []
            login_calls: list[dict] = []

            tweet = SimpleNamespace(
                id="cookie-1",
                text="$MSFT improving demand",
                created_at_datetime=datetime(2026, 3, 26, 10, 0, tzinfo=timezone.utc),
                user=SimpleNamespace(screen_name="cookie-user"),
                favorite_count=5,
                retweet_count=1,
                reply_count=1,
                lang="en",
            )

            class FakeTwikitClient:
                def __init__(self, language="en-US", proxy=None):
                    self.language = language
                    self.proxy = proxy
                    self.loaded_path = None
                    self.last_product = None
                    created_clients.append(self)

                def load_cookies(self, path):
                    self.loaded_path = path

                async def login(self, **kwargs):
                    login_calls.append(kwargs)

                async def search_tweet(self, query, product, count):
                    self.last_product = product
                    return [tweet]

            fake_module = SimpleNamespace(Client=FakeTwikitClient)
            client = XClient(pipeline_config.social, storage=storage)

            with patch("market_sentiment.sources.x_provider.importlib.import_module", return_value=fake_module):
                result = client.fetch_posts("MSFT", "Microsoft Corporation", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 1)
            self.assertEqual(len(login_calls), 0)
            self.assertEqual(created_clients[0].proxy, "http://proxy.local:8080")
            self.assertEqual(created_clients[0].loaded_path, str(cookies_path))
            self.assertEqual(created_clients[0].last_product, "Top")

    def test_alpha_vantage_options_parses_chain_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["ALPHAVANTAGE_API_KEY"] = "demo"
            payload = {
                "endpoint": "Realtime Options",
                "data": [
                    {
                        "contractID": "MSFT260417C00400000",
                        "symbol": "MSFT",
                        "expiration": "2026-04-17",
                        "strike": "400.00",
                        "type": "call",
                        "last": "12.40",
                        "mark": "12.45",
                        "bid": "12.30",
                        "ask": "12.60",
                        "volume": "220",
                        "open_interest": "1400",
                        "implied_volatility": "0.31",
                        "date": "2026-03-26",
                    },
                    {
                        "contractID": "MSFT260417P00380000",
                        "symbol": "MSFT",
                        "expiration": "2026-04-17",
                        "strike": "380.00",
                        "type": "put",
                        "last": "8.10",
                        "mark": "8.15",
                        "bid": "8.00",
                        "ask": "8.30",
                        "volume": "120",
                        "open_interest": "900",
                        "implied_volatility": "0.34",
                        "date": "2026-03-26",
                    },
                ],
            }
            http = FakeHttpClient(payload)
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            client = AlphaVantageOptionsClient(http, storage, OptionsConfig(enabled=True))

            with patch("market_sentiment.sources.options_alpha_vantage._current_utc_date", return_value=date(2026, 3, 26)):
                result = client.fetch_realtime_chain("MSFT", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertIsNotNone(result.data)
            self.assertEqual(http.last_params["function"], "REALTIME_OPTIONS")
            self.assertEqual(result.data.contract_count, 2)
            self.assertEqual(result.data.call_contracts, 1)
            self.assertEqual(result.data.put_contracts, 1)
            self.assertAlmostEqual(result.data.put_call_volume_ratio, 120 / 220, places=4)
            self.assertEqual(result.data.top_contracts[0].contract_id, "MSFT260417C00400000")

    def test_alpha_vantage_options_uses_historical_endpoint_for_backfill_and_respects_greeks_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["ALPHAVANTAGE_API_KEY"] = "demo"
            payload = {
                "endpoint": "Historical Options",
                "data": [
                    {
                        "contractID": "MSFT260417C00400000",
                        "symbol": "MSFT",
                        "expiration": "2026-04-17",
                        "strike": "400.00",
                        "type": "call",
                        "last": "12.40",
                        "mark": "12.45",
                        "bid": "12.30",
                        "ask": "12.60",
                        "volume": "220",
                        "open_interest": "1400",
                        "implied_volatility": "0.31",
                        "date": "2026-03-26",
                    }
                ],
            }
            http = FakeHttpClient(payload)
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            client = AlphaVantageOptionsClient(http, storage, OptionsConfig(enabled=True, require_greeks=True))

            with patch("market_sentiment.sources.options_alpha_vantage._current_utc_date", return_value=date(2026, 4, 2)):
                result = client.fetch_realtime_chain("MSFT", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(http.last_params["function"], "HISTORICAL_OPTIONS")
            self.assertEqual(http.last_params["date"], "2026-03-26")
            self.assertEqual(http.last_params["require_greeks"], "true")

    def test_alpha_vantage_options_rejects_unsupported_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["ALPHAVANTAGE_API_KEY"] = "demo"
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            client = AlphaVantageOptionsClient(FakeHttpClient({}), storage, OptionsConfig(enabled=True, provider="other"))

            result = client.fetch_realtime_chain("MSFT", date(2026, 3, 26))

            self.assertFalse(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertIn("unsupported options provider", result.status.message)

    def test_alpha_vantage_options_reports_premium_requirement_without_using_demo_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["ALPHAVANTAGE_API_KEY"] = "demo"
            payload = {
                "endpoint": "Realtime Options",
                "message": "This is a premium endpoint. Sample schema only.",
                "data": [
                    {
                        "contractID": "FAKE",
                        "symbol": "MSFT",
                        "expiration": "2099-99-99",
                        "strike": "20.00",
                        "type": "call",
                        "last": "100.00",
                        "volume": "100",
                        "open_interest": "100",
                        "date": "2049-99-99",
                    }
                ],
            }
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            client = AlphaVantageOptionsClient(FakeHttpClient(payload), storage, OptionsConfig(enabled=True))

            result = client.fetch_realtime_chain("MSFT", date(2026, 3, 26))

            self.assertFalse(result.status.success)
            self.assertTrue(result.status.partial)
            self.assertIsNone(result.data)
            self.assertIn("premium endpoint", result.status.message.lower())

    def test_pipeline_uses_stooq_fallback_when_primary_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MARKET_SENTIMENT_DATA_DIR"] = tmp
            os.environ["MARKET_SENTIMENT_DB_PATH"] = str(Path(tmp) / "market_sentiment.db")
            pipeline = DailyPipeline(load_config(str(CONFIG_PATH)))
            pipeline.config.social.enabled = False

            def primary_failure(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                return SourcePayload(data=[], status=SourceStatus(source="alpha_vantage", success=False, partial=True, message="missing key"))

            def stooq_success(ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
                return SourcePayload(
                    data=[
                        PriceBar(
                            ticker=ticker,
                            trading_date=date(2026, 3, 26),
                            open=100.0,
                            high=101.0,
                            low=99.0,
                            close=100.5,
                            volume=1000,
                            source="stooq",
                        )
                    ],
                    status=SourceStatus(source="stooq", success=True, message="ok"),
                )

            pipeline.alpha_vantage.fetch_daily_prices = primary_failure  # type: ignore[method-assign]
            pipeline.stooq.fetch_daily_prices = stooq_success  # type: ignore[method-assign]

            payload, statuses = pipeline._fetch_prices_with_fallback("NVDA", date(2026, 3, 26))

            self.assertEqual(payload.data[0].source, "stooq")
            self.assertEqual(len(statuses), 2)

    def test_companyfacts_pair_uses_comparable_reporting_period(self) -> None:
        us_gaap = {
            "Revenues": {
                "units": {
                    "USD": [
                        {"form": "10-Q", "end": "2026-03-31", "filed": "2026-04-25", "val": 120.0, "fy": 2026, "fp": "Q1"},
                        {"form": "10-K", "end": "2025-12-31", "filed": "2026-02-10", "val": 400.0, "fy": 2025, "fp": "FY"},
                        {"form": "10-Q", "end": "2025-03-31", "filed": "2025-04-25", "val": 100.0, "fy": 2025, "fp": "Q1"},
                    ]
                }
            }
        }

        latest, previous, meta = _extract_latest_pair(us_gaap, ["Revenues"])

        self.assertEqual(latest, 120.0)
        self.assertEqual(previous, 100.0)
        self.assertTrue(meta["comparable"])

    def test_sec_recent_events_prefers_primary_doc_description_for_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payloads = {
                "https://www.sec.gov/files/company_tickers_exchange.json": {
                    "data": [[1045810, "NVIDIA CORP", "NVDA", "Nasdaq"]],
                },
                "https://data.sec.gov/submissions/CIK0001045810.json": {
                    "filings": {
                        "recent": {
                            "form": ["8-K"],
                            "filingDate": ["2026-03-20"],
                            "accessionNumber": ["0001045810-26-000001"],
                            "acceptanceDateTime": ["20260320120000"],
                            "primaryDocument": ["nvda_8k.htm"],
                            "primaryDocDescription": ["Current report announcing partnership"],
                        }
                    }
                },
            }
            http = RoutingFakeHttpClient(payloads)
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            client = SecClient(http, storage)

            result = client.fetch_recent_events("NVDA", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(result.data[0].title, "Current report announcing partnership")

    def test_sec_ticker_map_failure_surfaces_upstream_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            http = FailingTickerMapHttpClient()
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            client = SecClient(http, storage)

            events_result = client.fetch_recent_events("NVDA", date(2026, 3, 26))
            facts_result = client.fetch_company_facts("NVDA", date(2026, 3, 26))

            self.assertFalse(events_result.status.success)
            self.assertFalse(facts_result.status.success)
            self.assertIn("Failed to load SEC ticker map", events_result.status.message)
            self.assertIn("Failed to load SEC ticker map", facts_result.status.message)
            self.assertNotIn("No CIK mapping", events_result.status.message)
            self.assertNotIn("No CIK mapping", facts_result.status.message)
