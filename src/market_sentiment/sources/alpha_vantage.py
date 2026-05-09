from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone

from market_sentiment.http import HttpClient
from market_sentiment.models import PriceBar, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage


class AlphaVantageClient:
    base_url = "https://www.alphavantage.co/query"
    _minimum_interval_seconds = 1.1
    _last_request_monotonic = 0.0
    _daily_limit_exhausted = False

    def __init__(self, http_client: HttpClient, storage: Storage) -> None:
        self._http = http_client
        self._storage = storage
        self._api_key = os.environ.get("ALPHAVANTAGE_API_KEY")

    def fetch_daily_prices(self, ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
        ingested_at = datetime.now(timezone.utc)
        if not self._api_key:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="alpha_vantage",
                    success=False,
                    partial=True,
                    message="ALPHAVANTAGE_API_KEY is not set",
                    ingested_at=ingested_at,
                ),
            )
        if self._daily_limit_exhausted:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="alpha_vantage",
                    success=False,
                    partial=True,
                    message="Alpha Vantage skipped after daily rate limit was previously reached in this process",
                    ingested_at=ingested_at,
                ),
            )

        elapsed = time.monotonic() - self._last_request_monotonic
        if elapsed < self._minimum_interval_seconds:
            time.sleep(self._minimum_interval_seconds - elapsed)
        response = self._http.get(
            self.base_url,
            params={
                "function": "TIME_SERIES_DAILY",
                "symbol": ticker,
                "apikey": self._api_key,
                "outputsize": "compact",
            },
        )
        safe_url = getattr(response, "safe_url", response.url)
        self._last_request_monotonic = time.monotonic()
        payload = response.json()
        if payload.get("Note") or payload.get("Information") or payload.get("Error Message"):
            message = payload.get("Note") or payload.get("Information") or payload.get("Error Message")
            if "25 requests per day" in message:
                self._daily_limit_exhausted = True
            raw_path = self._storage.write_raw_json(run_date, "alpha_vantage", ticker.lower(), payload)
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="alpha_vantage",
                    success=False,
                    partial=True,
                    message=message,
                    payload_path=str(raw_path),
                    source_url=safe_url,
                    ingested_at=ingested_at,
                ),
                raw_path=raw_path,
            )
        raw_path = self._storage.write_raw_json(run_date, "alpha_vantage", ticker.lower(), payload)
        time_series = payload.get("Time Series (Daily)", {})
        prices: list[PriceBar] = []
        for trading_day, values in sorted(time_series.items()):
            prices.append(
                PriceBar(
                    ticker=ticker,
                    trading_date=datetime.strptime(trading_day, "%Y-%m-%d").date(),
                    open=float(values["1. open"]),
                    high=float(values["2. high"]),
                    low=float(values["3. low"]),
                    close=float(values["4. close"]),
                    volume=float(values.get("5. volume", 0.0)),
                    source="alpha_vantage",
                    source_url=safe_url,
                    ingested_at=ingested_at,
                )
            )
        status = SourceStatus(
            source="alpha_vantage",
            success=bool(prices),
            partial=not bool(prices),
            message="ok" if prices else "empty response",
            payload_path=str(raw_path),
            source_url=safe_url,
            ingested_at=ingested_at,
            event_start=datetime.combine(prices[0].trading_date, datetime.min.time(), tzinfo=timezone.utc) if prices else None,
            event_end=datetime.combine(prices[-1].trading_date, datetime.min.time(), tzinfo=timezone.utc) if prices else None,
        )
        return SourcePayload(data=prices, status=status, raw_path=raw_path)
