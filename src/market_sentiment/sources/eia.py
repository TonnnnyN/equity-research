from __future__ import annotations

import os
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlsplit

from market_sentiment.http import HttpClient
from market_sentiment.models import MacroObservation, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage


class EiaClient:
    series_url = "https://api.eia.gov/v2/seriesid/{series_id}"
    route_base_url = "https://api.eia.gov"

    def __init__(self, http_client: HttpClient, storage: Storage) -> None:
        self._http = http_client
        self._storage = storage
        self._api_key = os.environ.get("EIA_API_KEY")

    def fetch_series(self, name: str, series_id: str, run_date: date) -> SourcePayload[list[MacroObservation]]:
        ingested_at = datetime.now(timezone.utc)
        if not self._api_key:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="eia",
                    success=False,
                    partial=True,
                    message="EIA_API_KEY is not set",
                    ingested_at=ingested_at,
                ),
            )

        payload, response_url = self._fetch_payload(series_id)
        safe_stem = name.lower().replace(" ", "_")
        raw_path = self._storage.write_raw_json(run_date, "eia", safe_stem, payload)
        data = payload.get("response", {}).get("data", [])
        observations = []
        skipped_period_rows = 0
        for row in data:
            period = row.get("period")
            value = row.get("value")
            if value is None:
                # Some routes expose "price" rather than "value".
                value = row.get("price")
            if not period or value in (None, ""):
                continue
            try:
                observed_on = _parse_eia_period(period)
            except ValueError:
                skipped_period_rows += 1
                continue
            observations.append(
                MacroObservation(
                    name=name,
                    observed_on=observed_on,
                    value=float(value),
                    source="eia",
                    source_url=response_url,
                    ingested_at=ingested_at,
                )
            )
        observed_dates = [observation.observed_on for observation in observations]
        status = SourceStatus(
            source="eia",
            success=bool(observations),
            partial=not bool(observations),
            message="ok" if observations and not skipped_period_rows else (
                f"ok (skipped {skipped_period_rows} rows with unsupported period format)"
                if observations
                else "empty observations"
            ),
            payload_path=str(raw_path),
            source_url=response_url,
            ingested_at=ingested_at,
            event_start=datetime.combine(min(observed_dates), datetime.min.time(), tzinfo=timezone.utc) if observed_dates else None,
            event_end=datetime.combine(max(observed_dates), datetime.min.time(), tzinfo=timezone.utc) if observed_dates else None,
        )
        return SourcePayload(data=observations, status=status, raw_path=raw_path)

    def _fetch_payload(self, route_or_series: str) -> tuple[dict, str]:
        if route_or_series.startswith("/"):
            route = route_or_series
            split = urlsplit(route)
            params = dict(parse_qsl(split.query, keep_blank_values=True))
            params.setdefault("api_key", self._api_key)
            params.setdefault("data[0]", "value")
            params.setdefault("sort[0][column]", "period")
            params.setdefault("sort[0][direction]", "desc")
            params.setdefault("offset", "0")
            params.setdefault("length", "24")
            response = self._http.get(f"{self.route_base_url}{split.path}", params=params)
            return response.json(), getattr(response, "safe_url", response.url)

        response = self._http.get(
            self.series_url.format(series_id=route_or_series),
            params={"api_key": self._api_key},
        )
        return response.json(), getattr(response, "safe_url", response.url)


def _parse_eia_period(period: str) -> date:
    for pattern in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(period, pattern)
            if pattern == "%Y":
                return date(parsed.year, 1, 1)
            if pattern == "%Y-%m":
                return date(parsed.year, parsed.month, 1)
            return parsed.date()
        except ValueError:
            continue
    raise ValueError(f"Unsupported EIA period format: {period}")
