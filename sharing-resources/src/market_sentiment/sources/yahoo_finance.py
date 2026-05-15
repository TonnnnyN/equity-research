from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.parse import quote

from market_sentiment.http import HttpClient
from market_sentiment.models import PriceBar, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage


class YahooFinanceClient:
    base_url = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

    def __init__(self, http_client: HttpClient, storage: Storage) -> None:
        self._http = http_client
        self._storage = storage

    def fetch_daily_prices(self, ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
        """Fetch daily prices from Yahoo Finance Chart API.

        Args:
            ticker: Stock ticker (e.g., 'MSFT', '9660.HK')
            run_date: Reference date for the fetch

        Returns:
            SourcePayload with list of PriceBar objects or empty list on failure
        """
        ingested_at = datetime.now(timezone.utc)

        # URL encode ticker to handle special characters (.HK, etc.)
        encoded_ticker = quote(ticker, safe="")
        url = self.base_url.format(ticker=encoded_ticker)

        # Fetch with 6-month range to cover ~120 trading days
        try:
            response = self._http.get(
                url,
                params={
                    "interval": "1d",
                    "range": "6mo",
                    "events": "div,splits",
                    "includePrePost": "false",
                },
            )
        except Exception as exc:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="yahoo_chart",
                    success=False,
                    partial=True,
                    message=f"Failed to fetch from Yahoo Finance: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        try:
            payload = response.json()
        except Exception as exc:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="yahoo_chart",
                    success=False,
                    partial=True,
                    message=f"Failed to parse Yahoo Finance response: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        # Write raw payload to storage
        try:
            raw_path = self._storage.write_raw_json(run_date, "yahoo_chart", ticker.lower(), payload)
        except Exception as exc:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="yahoo_chart",
                    success=False,
                    partial=True,
                    message=f"Failed to write raw payload: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        # Check for API errors in response
        try:
            chart = payload.get("chart", {})
            error = chart.get("error")
            if error:
                error_msg = error.get("description", str(error))
                return SourcePayload(
                    data=[],
                    status=SourceStatus(
                        source="yahoo_chart",
                        success=False,
                        partial=True,
                        message=error_msg,
                        payload_path=str(raw_path) if raw_path else None,
                        source_url=response.url if hasattr(response, "url") else None,
                        ingested_at=ingested_at,
                    ),
                    raw_path=raw_path,
                )

            # Extract data from result
            results = chart.get("result", [])
            if not results:
                return SourcePayload(
                    data=[],
                    status=SourceStatus(
                        source="yahoo_chart",
                        success=False,
                        partial=True,
                        message="empty response",
                        payload_path=str(raw_path) if raw_path else None,
                        source_url=response.url if hasattr(response, "url") else None,
                        ingested_at=ingested_at,
                    ),
                    raw_path=raw_path,
                )

            result = results[0]
            timestamps = result.get("timestamp", [])
            indicators = result.get("indicators", {})
            quotes = indicators.get("quote", [{}])[0]

            if not timestamps or not quotes:
                return SourcePayload(
                    data=[],
                    status=SourceStatus(
                        source="yahoo_chart",
                        success=False,
                        partial=True,
                        message="empty response",
                        payload_path=str(raw_path) if raw_path else None,
                        source_url=response.url if hasattr(response, "url") else None,
                        ingested_at=ingested_at,
                    ),
                    raw_path=raw_path,
                )

            # Parse OHLCV data
            opens = quotes.get("open", [])
            highs = quotes.get("high", [])
            lows = quotes.get("low", [])
            closes = quotes.get("close", [])
            volumes = quotes.get("volume", [])
            # Extract adjusted closes when available; fall back to unadjusted closes
            adjclose_blocks = indicators.get("adjclose", [])
            adjcloses = adjclose_blocks[0].get("adjclose", []) if adjclose_blocks else []

            prices: list[PriceBar] = []
            safe_url = getattr(response, "safe_url", getattr(response, "url", None))

            for i, timestamp in enumerate(timestamps):
                # Prefer adjusted close when available; fall back to unadjusted close
                close = closes[i] if i < len(closes) else None
                if i < len(adjcloses) and adjcloses[i] is not None:
                    close = adjcloses[i]

                if close is None or close == 0:
                    continue

                trading_date = datetime.fromtimestamp(timestamp, tz=timezone.utc).date()

                price_bar = PriceBar(
                    ticker=ticker,
                    trading_date=trading_date,
                    open=float(opens[i]) if i < len(opens) and opens[i] is not None else 0.0,
                    high=float(highs[i]) if i < len(highs) and highs[i] is not None else 0.0,
                    low=float(lows[i]) if i < len(lows) and lows[i] is not None else 0.0,
                    close=float(close),
                    volume=float(volumes[i]) if i < len(volumes) and volumes[i] is not None else None,
                    source="yahoo_chart",
                    source_url=safe_url,
                    ingested_at=ingested_at,
                )
                prices.append(price_bar)

            if not prices:
                return SourcePayload(
                    data=[],
                    status=SourceStatus(
                        source="yahoo_chart",
                        success=False,
                        partial=True,
                        message="empty response",
                        payload_path=str(raw_path) if raw_path else None,
                        source_url=safe_url,
                        ingested_at=ingested_at,
                    ),
                    raw_path=raw_path,
                )

            # Sort prices by trading_date (ascending)
            prices.sort(key=lambda p: p.trading_date)

            # Build final status
            status = SourceStatus(
                source="yahoo_chart",
                success=True,
                partial=False,
                message="ok",
                payload_path=str(raw_path) if raw_path else None,
                source_url=safe_url,
                ingested_at=ingested_at,
                event_start=datetime.combine(
                    prices[0].trading_date, datetime.min.time(), tzinfo=timezone.utc
                ),
                event_end=datetime.combine(
                    prices[-1].trading_date, datetime.min.time(), tzinfo=timezone.utc
                ),
            )

            return SourcePayload(data=prices, status=status, raw_path=raw_path)

        except Exception as exc:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="yahoo_chart",
                    success=False,
                    partial=True,
                    message=f"Failed to parse Yahoo Finance payload: {exc}",
                    payload_path=str(raw_path) if raw_path else None,
                    source_url=response.url if hasattr(response, "url") else None,
                    ingested_at=ingested_at,
                ),
                raw_path=raw_path,
            )
