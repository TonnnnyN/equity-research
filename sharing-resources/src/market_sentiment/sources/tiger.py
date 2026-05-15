from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import certifi as _certifi

from market_sentiment.http import HttpClient
from market_sentiment.models import PriceBar, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage

# Ensure tigeropen SDK (uses requests/urllib3) finds a valid CA bundle.
# Python 3.12 framework install on macOS ships without a default cafile;
# point SDK to certifi's bundle (same fix as http.py). Use setdefault so
# user-provided env values win.
os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", _certifi.where())


def _to_tiger_symbol(ticker: str) -> str:
    """Convert ticker to Tiger API symbol format.

    US stocks: AAPL -> AAPL (uppercase, no change)
    HK stocks: 9660.HK -> 09660.HK (pad to 5 digits if needed, ensure .HK suffix)
    """
    if ticker.endswith(".HK"):
        # Remove .HK suffix, pad to 5 digits, re-add suffix
        code = ticker[:-3]  # Remove '.HK'
        code_padded = code.zfill(5)
        return f"{code_padded}.HK"
    else:
        # US stock: just uppercase
        return ticker.upper()


class TigerClient:
    def __init__(self, http_client: HttpClient, storage: Storage) -> None:
        self._http = http_client
        self._storage = storage
        self._tiger_config_path = os.environ.get("TIGER_CONFIG_PATH")

    def fetch_daily_prices(self, ticker: str, run_date: date) -> SourcePayload[list[PriceBar]]:
        """Fetch daily K-line prices from Tiger API.

        Args:
            ticker: Stock ticker (e.g., 'AAPL', '9660.HK')
            run_date: Reference date for the fetch

        Returns:
            SourcePayload with list of PriceBar objects or empty list on failure
        """
        ingested_at = datetime.now(timezone.utc)

        # Check TIGER_CONFIG_PATH is set
        if not self._tiger_config_path:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="tiger",
                    success=False,
                    partial=True,
                    message="TIGER_CONFIG_PATH is not set",
                    ingested_at=ingested_at,
                ),
            )

        # Parse config path: could be a file or directory
        config_path = Path(self._tiger_config_path).expanduser().resolve()

        # Validate path exists
        if not config_path.exists():
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="tiger",
                    success=False,
                    partial=True,
                    message=f"TIGER_CONFIG_PATH path not found: {self._tiger_config_path}",
                    ingested_at=ingested_at,
                ),
            )

        # If it's a file, use its parent directory; if it's a directory, use it directly
        if config_path.is_file():
            if config_path.name != "tiger_openapi_config.properties":
                return SourcePayload(
                    data=[],
                    status=SourceStatus(
                        source="tiger",
                        success=False,
                        partial=True,
                        message=f"TIGER_CONFIG_PATH must point to tiger_openapi_config.properties file, got: {config_path.name}",
                        ingested_at=ingested_at,
                    ),
                )
            props_dir = config_path.parent
        elif config_path.is_dir():
            props_dir = config_path
            # Verify tiger_openapi_config.properties exists in this directory.
            # tiger_openapi_token.properties (user_token) may also reside here; SDK reads both automatically.
            props_file = props_dir / "tiger_openapi_config.properties"
            if not props_file.exists():
                return SourcePayload(
                    data=[],
                    status=SourceStatus(
                        source="tiger",
                        success=False,
                        partial=True,
                        message=f"tiger_openapi_config.properties not found under {props_dir}",
                        ingested_at=ingested_at,
                    ),
                )
        else:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="tiger",
                    success=False,
                    partial=True,
                    message=f"TIGER_CONFIG_PATH must be a file or directory, got: {config_path}",
                    ingested_at=ingested_at,
                ),
            )

        # Import tigeropen SDK (lazy import to avoid startup failures if SDK not installed)
        try:
            from tigeropen.common.consts import RightOption
            from tigeropen.tiger_open_config import TigerOpenClientConfig
            from tigeropen.quote.quote_client import QuoteClient
        except ImportError:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="tiger",
                    success=False,
                    partial=True,
                    message="tigeropen not installed; pip install tigeropen",
                    ingested_at=ingested_at,
                ),
            )

        # Initialize Tiger client
        try:
            config = TigerOpenClientConfig(props_path=str(props_dir))
            # is_grab_permission=False: skip the SDK's startup grab_quote_permission call,
            # which requires a real-time market data subscription (user_token). Historical
            # daily K-lines via get_bars() do not need this permission grab.
            client = QuoteClient(config, is_grab_permission=False)
        except Exception as exc:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="tiger",
                    success=False,
                    partial=True,
                    message=f"Failed to initialize Tiger client: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        # Convert ticker to Tiger symbol format
        tiger_symbol = _to_tiger_symbol(ticker)

        # Calculate date range: ~150 days back to cover ~100 trading days
        begin_date = run_date - timedelta(days=150)

        # Fetch daily K-line data
        try:
            bars_df = client.get_bars(
                symbols=[tiger_symbol],
                period="day",
                begin_time=begin_date.isoformat(),
                end_time=run_date.isoformat(),
                limit=251,  # Default limit covers up to ~1 year of trading days
                right=RightOption.br_forward,  # Back-adjust for splits/dividends to today's basis
            )
        except Exception as exc:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="tiger",
                    success=False,
                    partial=True,
                    message=f"Failed to fetch bars from Tiger API: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        # Write raw payload to storage (convert DataFrame to dict list)
        try:
            if bars_df is not None and not bars_df.empty:
                payload_dict = bars_df.to_dict("records")
            else:
                payload_dict = []
            raw_path = self._storage.write_raw_json(
                run_date, "tiger", tiger_symbol.lower(), payload_dict
            )
        except Exception as exc:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="tiger",
                    success=False,
                    partial=True,
                    message=f"Failed to write raw payload: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        # Parse DataFrame into PriceBar objects
        if bars_df is None or bars_df.empty:
            # No data available from Tiger (may be insufficient subscription or symbol not found)
            status = SourceStatus(
                source="tiger",
                success=False,
                partial=True,
                message="empty response from Tiger API (check symbol and subscription level)",
                payload_path=str(raw_path) if raw_path else None,
                source_url=None,
                ingested_at=ingested_at,
            )
            return SourcePayload(data=[], status=status, raw_path=raw_path)

        prices: list[PriceBar] = []
        try:
            for _, row in bars_df.iterrows():
                # Tiger API returns 'time' as millisecond Unix timestamp
                # Convert to date: divide by 1000 to get seconds, then to datetime
                timestamp_ms = int(row.get("time", 0))
                timestamp_sec = timestamp_ms / 1000.0
                trading_date = datetime.fromtimestamp(timestamp_sec, tz=timezone.utc).date()

                price_bar = PriceBar(
                    ticker=ticker,  # Use original ticker (not tiger_symbol)
                    trading_date=trading_date,
                    open=float(row.get("open", 0.0)),
                    high=float(row.get("high", 0.0)),
                    low=float(row.get("low", 0.0)),
                    close=float(row.get("close", 0.0)),
                    volume=float(row.get("volume")) if row.get("volume") else None,
                    source="tiger",
                    source_url=None,  # Tiger API doesn't provide a URL for specific data
                    ingested_at=ingested_at,
                )
                prices.append(price_bar)
        except Exception as exc:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="tiger",
                    success=False,
                    partial=True,
                    message=f"Failed to parse Tiger response: {exc}",
                    payload_path=str(raw_path) if raw_path else None,
                    source_url=None,
                    ingested_at=ingested_at,
                ),
            )

        # Sort prices by trading_date (ascending)
        prices.sort(key=lambda p: p.trading_date)

        # Build final status
        status = SourceStatus(
            source="tiger",
            success=bool(prices),
            partial=not bool(prices),
            message="ok" if prices else "no bars returned",
            payload_path=str(raw_path) if raw_path else None,
            source_url=None,
            ingested_at=ingested_at,
            event_start=datetime.combine(
                prices[0].trading_date, datetime.min.time(), tzinfo=timezone.utc
            )
            if prices
            else None,
            event_end=datetime.combine(
                prices[-1].trading_date, datetime.min.time(), tzinfo=timezone.utc
            )
            if prices
            else None,
        )

        return SourcePayload(data=prices, status=status, raw_path=raw_path)
