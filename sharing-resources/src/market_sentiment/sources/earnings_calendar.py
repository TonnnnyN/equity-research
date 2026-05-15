from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.parse import quote

from market_sentiment.http import HttpClient
from market_sentiment.models import EarningsCalendar, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage


class EarningsCalendarClient:
    """Fetch next earnings date from Yahoo Finance quoteSummary endpoint."""

    base_url = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"

    def __init__(self, http_client: HttpClient, storage: Storage) -> None:
        self._http = http_client
        self._storage = storage

    def fetch_next_earnings(self, ticker: str, run_date: date) -> SourcePayload[EarningsCalendar | None]:
        """Fetch next earnings date from Yahoo Finance.

        Args:
            ticker: Stock ticker (e.g., 'MSFT', '9660.HK')
            run_date: Reference date for the fetch

        Returns:
            SourcePayload with EarningsCalendar object or None if no future date found
        """
        ingested_at = datetime.now(timezone.utc)

        # Skip non-US tickers (e.g., .HK, .L) — Yahoo may not have earnings calendars for them
        if "." in ticker and not ticker.endswith(".US"):
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="yahoo_earnings_calendar",
                    success=False,
                    partial=True,
                    message=f"Non-US ticker {ticker} not supported by Yahoo earnings calendar",
                    ingested_at=ingested_at,
                ),
            )

        # URL encode ticker
        encoded_ticker = quote(ticker, safe="")
        url = self.base_url.format(ticker=encoded_ticker)

        # Fetch from Yahoo Finance quoteSummary with calendarEvents module
        try:
            response = self._http.get(
                url,
                params={"modules": "calendarEvents"},
            )
        except Exception as exc:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="yahoo_earnings_calendar",
                    success=False,
                    partial=True,
                    message=f"Failed to fetch from Yahoo Finance: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        # Parse JSON response
        try:
            payload = response.json()
        except Exception as exc:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="yahoo_earnings_calendar",
                    success=False,
                    partial=True,
                    message=f"Failed to parse Yahoo Finance response: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        # Write raw payload to storage
        try:
            raw_path = self._storage.write_raw_json(run_date, "yahoo_earnings_calendar", ticker.lower(), payload)
        except Exception as exc:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="yahoo_earnings_calendar",
                    success=False,
                    partial=True,
                    message=f"Failed to write raw payload: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        # Check for API errors in response
        try:
            quote_summary = payload.get("quoteSummary", {})
            error = quote_summary.get("error")
            if error:
                error_msg = error.get("description", str(error))
                return SourcePayload(
                    data=None,
                    status=SourceStatus(
                        source="yahoo_earnings_calendar",
                        success=False,
                        partial=True,
                        message=error_msg,
                        payload_path=str(raw_path) if raw_path else None,
                        source_url=response.url if hasattr(response, "url") else None,
                        ingested_at=ingested_at,
                    ),
                    raw_path=raw_path,
                )

            # Extract calendarEvents from result
            results = quote_summary.get("result", [])
            if not results:
                # No data available for this ticker
                return SourcePayload(
                    data=None,
                    status=SourceStatus(
                        source="yahoo_earnings_calendar",
                        success=False,
                        partial=True,
                        message="no earnings calendar data available",
                        payload_path=str(raw_path) if raw_path else None,
                        source_url=response.url if hasattr(response, "url") else None,
                        ingested_at=ingested_at,
                    ),
                    raw_path=raw_path,
                )

            result = results[0]
            calendar_events = result.get("calendarEvents", {})
            earnings = calendar_events.get("earnings", {})

            # Extract earnings date information
            earnings_dates = earnings.get("earningsDate", [])
            if not earnings_dates:
                # No earnings date available
                return SourcePayload(
                    data=None,
                    status=SourceStatus(
                        source="yahoo_earnings_calendar",
                        success=False,
                        partial=True,
                        message="no earnings date found",
                        payload_path=str(raw_path) if raw_path else None,
                        source_url=response.url if hasattr(response, "url") else None,
                        ingested_at=ingested_at,
                    ),
                    raw_path=raw_path,
                )

            # Take the first earnings date
            first_earnings = earnings_dates[0]
            raw_timestamp = first_earnings.get("raw")
            fmt_date = first_earnings.get("fmt")

            if raw_timestamp is None:
                # No valid timestamp
                return SourcePayload(
                    data=None,
                    status=SourceStatus(
                        source="yahoo_earnings_calendar",
                        success=False,
                        partial=True,
                        message="earnings date missing timestamp",
                        payload_path=str(raw_path) if raw_path else None,
                        source_url=response.url if hasattr(response, "url") else None,
                        ingested_at=ingested_at,
                    ),
                    raw_path=raw_path,
                )

            # Convert unix epoch seconds to date
            earnings_date = datetime.fromtimestamp(raw_timestamp, tz=timezone.utc).date()

            # Filter out past dates — we want the NEXT earnings
            if earnings_date < run_date:
                return SourcePayload(
                    data=None,
                    status=SourceStatus(
                        source="yahoo_earnings_calendar",
                        success=False,
                        partial=True,
                        message=f"earnings date {earnings_date.isoformat()} is in the past",
                        payload_path=str(raw_path) if raw_path else None,
                        source_url=response.url if hasattr(response, "url") else None,
                        ingested_at=ingested_at,
                    ),
                    raw_path=raw_path,
                )

            # Check if the date is an estimate
            is_estimate = earnings.get("isEarningsDateEstimate", False)

            earnings_calendar = EarningsCalendar(
                ticker=ticker,
                next_earnings_date=earnings_date,
                is_estimate=is_estimate,
                fetched_at=ingested_at,
            )

            status = SourceStatus(
                source="yahoo_earnings_calendar",
                success=True,
                partial=False,
                message="ok",
                payload_path=str(raw_path) if raw_path else None,
                source_url=response.url if hasattr(response, "url") else None,
                ingested_at=ingested_at,
                event_start=datetime.combine(earnings_date, datetime.min.time(), tzinfo=timezone.utc),
                event_end=datetime.combine(earnings_date, datetime.min.time(), tzinfo=timezone.utc),
            )

            return SourcePayload(data=earnings_calendar, status=status, raw_path=raw_path)

        except Exception as exc:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="yahoo_earnings_calendar",
                    success=False,
                    partial=True,
                    message=f"Failed to parse Yahoo Finance payload: {exc}",
                    payload_path=str(raw_path) if raw_path else None,
                    source_url=response.url if hasattr(response, "url") else None,
                    ingested_at=ingested_at,
                ),
                raw_path=raw_path,
            )
