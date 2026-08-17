from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from unittest import TestCase
from unittest.mock import patch

from equity_research.config import load_config
from equity_research.http import HttpClient, redact_url
from equity_research.models import PriceBar, SourceStatus
from equity_research.pipeline import DailyPipeline
from equity_research.sources.base import SourcePayload
from equity_research.sources.sec import SecClient, _extract_latest_pair
from equity_research.storage import Storage
from equity_research.sources.social_base import matches_security_text


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.toml"


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

        with patch("equity_research.http.urlopen", side_effect=URLError("boom")):
            with self.assertRaisesRegex(RuntimeError, "api_key=REDACTED") as raised:
                client.get("https://example.com/data", params={"api_key": "secret", "series_id": "DGS10"})

        self.assertNotIn("secret", str(raised.exception))

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

    def test_sec_filing_cache_first_run_writes_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payloads = {
                "https://www.sec.gov/files/company_tickers_exchange.json": {
                    "data": [[789019, "MICROSOFT CORP", "MSFT", "NASDAQ"]],
                },
                "https://data.sec.gov/submissions/CIK0000789019.json": {
                    "filings": {
                        "recent": {
                            "form": ["10-K", "10-Q", "8-K"],
                            "filingDate": ["2026-02-20", "2026-01-15", "2026-03-10"],
                            "accessionNumber": ["0000789019-26-000001", "0000789019-26-000002", "0000789019-26-000003"],
                            "acceptanceDateTime": ["20260220090000", "20260115100000", "20260310110000"],
                            "primaryDocument": ["msft_10k.htm", "msft_10q.htm", "msft_8k.htm"],
                            "primaryDocDescription": ["Annual report", "Quarterly report", "Current report"],
                        }
                    }
                },
            }
            http = RoutingFakeHttpClient(payloads)
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            client = SecClient(http, storage)

            result = client.fetch_recent_events("MSFT", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 3)
            cached = storage.get_filing_summaries_for_ticker("MSFT")
            self.assertEqual(len(cached), 3)
            self.assertIn("cache_hit=0/3", result.status.message)

    def test_sec_filing_cache_second_run_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payloads = {
                "https://www.sec.gov/files/company_tickers_exchange.json": {
                    "data": [[789019, "MICROSOFT CORP", "MSFT", "NASDAQ"]],
                },
                "https://data.sec.gov/submissions/CIK0000789019.json": {
                    "filings": {
                        "recent": {
                            "form": ["10-K", "10-Q", "8-K"],
                            "filingDate": ["2026-02-20", "2026-01-15", "2026-03-10"],
                            "accessionNumber": ["0000789019-26-000001", "0000789019-26-000002", "0000789019-26-000003"],
                            "acceptanceDateTime": ["20260220090000", "20260115100000", "20260310110000"],
                            "primaryDocument": ["msft_10k.htm", "msft_10q.htm", "msft_8k.htm"],
                            "primaryDocDescription": ["Annual report", "Quarterly report", "Current report"],
                        }
                    }
                },
            }
            http = RoutingFakeHttpClient(payloads)
            storage = Storage(Path(tmp) / "state.db", Path(tmp))
            storage.init_db()
            client = SecClient(http, storage)

            from equity_research.models import FilingSummaryCacheRow
            storage.upsert_filing_summary_cache([
                FilingSummaryCacheRow(
                    cik="0000789019",
                    accession_number="0000789019-26-000001",
                    ticker="MSFT",
                    form_type="10-K",
                    filed_at=datetime(2026, 2, 20, tzinfo=timezone.utc),
                    period_end=None,
                    summary="Annual report",
                    sentiment="unknown",
                    key_metrics_json="{}",
                    ingested_at=datetime.now(timezone.utc),
                ),
                FilingSummaryCacheRow(
                    cik="0000789019",
                    accession_number="0000789019-26-000002",
                    ticker="MSFT",
                    form_type="10-Q",
                    filed_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
                    period_end=None,
                    summary="Quarterly report",
                    sentiment="unknown",
                    key_metrics_json="{}",
                    ingested_at=datetime.now(timezone.utc),
                ),
            ])

            result = client.fetch_recent_events("MSFT", date(2026, 3, 26))

            self.assertTrue(result.status.success)
            self.assertEqual(len(result.data), 3)
            cached = storage.get_filing_summaries_for_ticker("MSFT")
            self.assertEqual(len(cached), 3)
            self.assertIn("cache_hit=2/3", result.status.message)
