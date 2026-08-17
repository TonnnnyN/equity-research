from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone

from equity_research.http import HttpClient
from equity_research.models import PriceBar, SourceStatus
from equity_research.sources.base import SourcePayload
from equity_research.storage import Storage


class StooqClient:
    base_url = "https://stooq.com/q/d/l/"

    def __init__(self, http_client: HttpClient, storage: Storage) -> None:
        self._http = http_client
        self._storage = storage

    def fetch_daily_prices(self, ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
        symbol = f"{ticker.lower()}.us"
        response = self._http.get(self.base_url, params={"s": symbol, "i": "d"})
        safe_url = getattr(response, "safe_url", response.url)
        payload_text = response.text()
        raw_path = self._storage.write_raw_text(run_date, "stooq", ticker.lower(), payload_text)
        reader = csv.DictReader(io.StringIO(payload_text))
        ingested_at = datetime.now(timezone.utc)
        prices: list[PriceBar] = []
        for row in reader:
            if not row.get("Date") or row.get("Close") in (None, "", "0"):
                continue
            prices.append(
                PriceBar(
                    ticker=ticker,
                    trading_date=datetime.strptime(row["Date"], "%Y-%m-%d").date(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row["Volume"]) if row.get("Volume") else None,
                    source="stooq",
                    source_url=safe_url,
                    ingested_at=ingested_at,
                )
            )
        status = SourceStatus(
            source="stooq",
            success=bool(prices),
            partial=not bool(prices),
            message="ok" if prices else "empty csv response",
            payload_path=str(raw_path),
            source_url=safe_url,
            ingested_at=ingested_at,
            event_start=datetime.combine(prices[0].trading_date, datetime.min.time(), tzinfo=timezone.utc) if prices else None,
            event_end=datetime.combine(prices[-1].trading_date, datetime.min.time(), tzinfo=timezone.utc) if prices else None,
        )
        return SourcePayload(data=prices, status=status, raw_path=raw_path)
