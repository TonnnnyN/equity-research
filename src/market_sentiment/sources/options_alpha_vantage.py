from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone
from typing import Any

from market_sentiment.config import OptionsConfig
from market_sentiment.http import HttpClient
from market_sentiment.models import OptionContract, OptionSnapshot, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage


class AlphaVantageOptionsClient:
    base_url = "https://www.alphavantage.co/query"
    _minimum_interval_seconds = 1.1
    _last_request_monotonic = 0.0

    def __init__(
        self,
        http_client: HttpClient,
        storage: Storage,
        config: OptionsConfig | None = None,
    ) -> None:
        self._http = http_client
        self._storage = storage
        self._config = config or OptionsConfig()
        self._api_key = os.environ.get("ALPHAVANTAGE_API_KEY")

    def fetch_realtime_chain(self, ticker: str, run_date: date) -> SourcePayload[OptionSnapshot | None]:
        ingested_at = datetime.now(timezone.utc)
        if not self._config.enabled:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="alpha_vantage_options",
                    success=True,
                    message="options provider disabled",
                    ingested_at=ingested_at,
                ),
            )
        if self._config.provider != "alpha_vantage":
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="alpha_vantage_options",
                    success=False,
                    partial=True,
                    message=f"unsupported options provider: {self._config.provider}",
                    ingested_at=ingested_at,
                ),
            )
        if not self._api_key:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="alpha_vantage_options",
                    success=False,
                    partial=True,
                    message="ALPHAVANTAGE_API_KEY is not set",
                    ingested_at=ingested_at,
                ),
            )

        elapsed = time.monotonic() - self._last_request_monotonic
        if elapsed < self._minimum_interval_seconds:
            time.sleep(self._minimum_interval_seconds - elapsed)

        response = self._http.get(self.base_url, params=_request_params(self._config, ticker, run_date, self._api_key))
        safe_url = getattr(response, "safe_url", response.url)
        self._last_request_monotonic = time.monotonic()
        payload = response.json()
        raw_path = self._storage.write_raw_json(run_date, "alpha_vantage_options", ticker.lower(), payload)

        upstream_message = _upstream_message(payload)
        if upstream_message and _looks_like_premium_gate(upstream_message):
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="alpha_vantage_options",
                    success=False,
                    partial=True,
                    message=upstream_message,
                    payload_path=str(raw_path),
                    source_url=safe_url,
                    ingested_at=ingested_at,
                ),
                raw_path=raw_path,
            )

        contracts = _parse_contracts(payload.get("data"), max_contracts=self._config.max_contracts)
        if not contracts:
            message = upstream_message or "empty options response"
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="alpha_vantage_options",
                    success=False,
                    partial=True,
                    message=message,
                    payload_path=str(raw_path),
                    source_url=safe_url,
                    ingested_at=ingested_at,
                ),
                raw_path=raw_path,
            )

        snapshot = _build_snapshot(
            ticker=ticker,
            run_date=run_date,
            contracts=contracts,
            safe_url=safe_url,
            raw_path=str(raw_path),
            ingested_at=ingested_at,
        )
        return SourcePayload(
            data=snapshot,
            status=SourceStatus(
                source="alpha_vantage_options",
                success=True,
                partial=False,
                message="ok",
                payload_path=str(raw_path),
                source_url=safe_url,
                ingested_at=ingested_at,
            ),
            raw_path=raw_path,
        )


def _build_snapshot(
    *,
    ticker: str,
    run_date: date,
    contracts: list[OptionContract],
    safe_url: str,
    raw_path: str,
    ingested_at: datetime,
) -> OptionSnapshot:
    call_contracts = [contract for contract in contracts if contract.option_type == "call"]
    put_contracts = [contract for contract in contracts if contract.option_type == "put"]
    total_call_volume = sum(contract.volume or 0 for contract in call_contracts)
    total_put_volume = sum(contract.volume or 0 for contract in put_contracts)
    total_call_open_interest = sum(contract.open_interest or 0 for contract in call_contracts)
    total_put_open_interest = sum(contract.open_interest or 0 for contract in put_contracts)
    implied_vols = [contract.implied_volatility for contract in contracts if contract.implied_volatility is not None]
    expirations = sorted({contract.expiration for contract in contracts})
    nearest_expiration = expirations[0] if expirations else None
    top_contracts = sorted(
        contracts,
        key=lambda item: (item.open_interest or 0, item.volume or 0, -(item.last_price or 0.0)),
        reverse=True,
    )[:5]
    return OptionSnapshot(
        ticker=ticker,
        run_date=run_date,
        source="alpha_vantage_options",
        contract_count=len(contracts),
        call_contracts=len(call_contracts),
        put_contracts=len(put_contracts),
        total_call_volume=total_call_volume,
        total_put_volume=total_put_volume,
        total_call_open_interest=total_call_open_interest,
        total_put_open_interest=total_put_open_interest,
        put_call_volume_ratio=_safe_ratio(total_put_volume, total_call_volume),
        put_call_open_interest_ratio=_safe_ratio(total_put_open_interest, total_call_open_interest),
        implied_volatility_avg=(sum(implied_vols) / len(implied_vols)) if implied_vols else None,
        nearest_expiration=nearest_expiration,
        nearest_days_to_expiry=(nearest_expiration - run_date).days if nearest_expiration else None,
        max_call_open_interest_strike=_max_open_interest_strike(call_contracts),
        max_put_open_interest_strike=_max_open_interest_strike(put_contracts),
        top_contracts=top_contracts,
        source_url=safe_url,
        raw_payload_path=raw_path,
        ingested_at=ingested_at,
    )


def _request_params(config: OptionsConfig, ticker: str, run_date: date, api_key: str) -> dict[str, str]:
    params = {
        "function": "REALTIME_OPTIONS" if run_date >= _current_utc_date() else "HISTORICAL_OPTIONS",
        "symbol": ticker,
        "apikey": api_key,
    }
    if run_date < _current_utc_date():
        params["date"] = run_date.isoformat()
    if config.require_greeks:
        params["require_greeks"] = "true"
    return params


def _parse_contracts(payload_data: Any, *, max_contracts: int) -> list[OptionContract]:
    if not isinstance(payload_data, list):
        return []
    contracts: list[OptionContract] = []
    for item in payload_data:
        if not isinstance(item, dict):
            continue
        contract = _parse_contract(item)
        if contract is None:
            continue
        contracts.append(contract)
    if max_contracts > 0:
        contracts = sorted(
            contracts,
            key=lambda item: (item.open_interest or 0, item.volume or 0),
            reverse=True,
        )[:max_contracts]
    return contracts


def _parse_contract(item: dict[str, Any]) -> OptionContract | None:
    contract_id = str(item.get("contractID") or item.get("contract_id") or "").strip()
    option_type = str(item.get("type") or item.get("option_type") or "").strip().lower()
    expiration = _parse_date(item.get("expiration"))
    strike = _parse_float(item.get("strike"))
    if not contract_id or option_type not in {"call", "put"} or expiration is None or strike is None:
        return None
    return OptionContract(
        contract_id=contract_id,
        expiration=expiration,
        strike=strike,
        option_type=option_type,
        volume=_parse_int(item.get("volume")),
        open_interest=_parse_int(item.get("open_interest")),
        implied_volatility=_parse_float(item.get("implied_volatility")),
        last_price=_parse_float(item.get("last")),
        bid=_parse_float(item.get("bid")),
        ask=_parse_float(item.get("ask")),
        mark=_parse_float(item.get("mark")),
        trade_date=_parse_date(item.get("date")),
    )


def _upstream_message(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("message", "Note", "Information", "Error Message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _looks_like_premium_gate(message: str) -> bool:
    normalized = message.lower()
    return "premium endpoint" in normalized or "sample schema" in normalized or "premium" in normalized


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int | None:
    parsed = _parse_float(value)
    if parsed is None:
        return None
    return int(parsed)


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _max_open_interest_strike(contracts: list[OptionContract]) -> float | None:
    if not contracts:
        return None
    top = max(contracts, key=lambda item: (item.open_interest or 0, item.volume or 0))
    return top.strike


def _current_utc_date() -> date:
    return datetime.now(timezone.utc).date()
