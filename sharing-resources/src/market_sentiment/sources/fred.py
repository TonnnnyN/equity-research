from __future__ import annotations

import os
from datetime import date, datetime, timezone

from market_sentiment.http import HttpClient
from market_sentiment.models import MacroObservation, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage


class FredClient:
    base_url = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(self, http_client: HttpClient, storage: Storage) -> None:
        self._http = http_client
        self._storage = storage
        self._api_key = os.environ.get("FRED_API_KEY")

    def fetch_series(self, name: str, series_id: str, run_date: date) -> SourcePayload[list[MacroObservation]]:
        ingested_at = datetime.now(timezone.utc)
        if not self._api_key:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="fred",
                    success=False,
                    partial=True,
                    message="FRED_API_KEY is not set",
                    ingested_at=ingested_at,
                ),
            )
        params = {"series_id": series_id, "file_type": "json"}
        params["api_key"] = self._api_key

        response = self._http.get(self.base_url, params=params)
        safe_url = getattr(response, "safe_url", response.url)
        payload = response.json()
        raw_path = self._storage.write_raw_json(run_date, "fred", f"{name.lower()}_{series_id.lower()}", payload)
        observations = []
        for item in payload.get("observations", []):
            value = item.get("value")
            if value in (None, "."):
                continue
            observations.append(
                MacroObservation(
                    name=name,
                    observed_on=datetime.strptime(item["date"], "%Y-%m-%d").date(),
                    value=float(value),
                    source="fred",
                    source_url=safe_url,
                    ingested_at=ingested_at,
                )
            )
        observed_dates = [observation.observed_on for observation in observations]
        status = SourceStatus(
            source="fred",
            success=bool(observations),
            partial=not bool(observations),
            message="ok" if observations else "empty observations",
            payload_path=str(raw_path),
            source_url=safe_url,
            ingested_at=ingested_at,
            event_start=datetime.combine(min(observed_dates), datetime.min.time(), tzinfo=timezone.utc) if observed_dates else None,
            event_end=datetime.combine(max(observed_dates), datetime.min.time(), tzinfo=timezone.utc) if observed_dates else None,
        )
        return SourcePayload(data=observations, status=status, raw_path=raw_path)
