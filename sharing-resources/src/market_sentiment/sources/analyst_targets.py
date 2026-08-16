from __future__ import annotations

import os
from datetime import date, datetime, timezone
from urllib.parse import quote

from market_sentiment.http import HttpClient
from market_sentiment.models import AnalystRatingChange, AnalystSnapshot, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage


class AnalystTargetsClient:
    """Fetch analyst price targets and rating momentum from Yahoo Finance quoteSummary.

    Falls back to Finnhub when Yahoo fails and ``FINNHUB_API_KEY`` is set.
    This is a Layer 2 advisory-evidence lane — it MUST NOT block the pipeline,
    MUST NOT affect scores, and MUST NOT mark partial_coverage.
    """

    yahoo_base_url = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
    finnhub_target_url = "https://finnhub.io/api/v1/stock/price-target"
    finnhub_recommend_url = "https://finnhub.io/api/v1/stock/recommendation"
    finnhub_upgrade_url = "https://finnhub.io/api/v1/stock/upgrade-downgrade"

    def __init__(self, http_client: HttpClient, storage: Storage) -> None:
        self._http = http_client
        self._storage = storage

    def fetch_analyst_snapshot(
        self, ticker: str, run_date: date
    ) -> SourcePayload[AnalystSnapshot | None]:
        """Fetch analyst price targets and rating momentum.

        Tries Yahoo Finance first; falls back to Finnhub when Yahoo fails and
        ``FINNHUB_API_KEY`` is set.  Never raises — all exceptions are caught and
        returned as a failed ``SourcePayload``.

        Args:
            ticker: Stock ticker (e.g., 'MSFT', '9660.HK')
            run_date: Reference date for the fetch

        Returns:
            SourcePayload with AnalystSnapshot or None on failure
        """
        ingested_at = datetime.now(timezone.utc)

        # Non-US tickers: skip Yahoo path, attempt Finnhub if key available
        if "." in ticker and not ticker.endswith(".US"):
            return self._try_finnhub(ticker, run_date, ingested_at, reason="non-us")

        # --- Primary: Yahoo Finance ---
        yahoo_result = self._fetch_yahoo(ticker, run_date, ingested_at)
        if yahoo_result.status.success:
            return yahoo_result

        # --- Fallback: Finnhub ---
        return self._try_finnhub(ticker, run_date, ingested_at, reason="yahoo-failed")

    # ------------------------------------------------------------------
    # Yahoo Finance path
    # ------------------------------------------------------------------

    def _fetch_yahoo(
        self, ticker: str, run_date: date, ingested_at: datetime
    ) -> SourcePayload[AnalystSnapshot | None]:
        encoded_ticker = quote(ticker, safe="")
        url = self.yahoo_base_url.format(ticker=encoded_ticker)

        try:
            response = self._http.get(
                url,
                params={"modules": "financialData,recommendationTrend,upgradeDowngradeHistory"},
            )
        except Exception as exc:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="analyst_targets",
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
                data=None,
                status=SourceStatus(
                    source="analyst_targets",
                    success=False,
                    partial=True,
                    message=f"Failed to parse Yahoo Finance response: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        try:
            raw_path = self._storage.write_raw_json(
                run_date, "analyst_targets", ticker.lower(), payload
            )
        except Exception as exc:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="analyst_targets",
                    success=False,
                    partial=True,
                    message=f"Failed to write raw payload: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        try:
            quote_summary = payload.get("quoteSummary", {})
            error = quote_summary.get("error")
            if error:
                error_msg = error.get("description", str(error))
                return SourcePayload(
                    data=None,
                    status=SourceStatus(
                        source="analyst_targets",
                        success=False,
                        partial=True,
                        message=error_msg,
                        payload_path=str(raw_path) if raw_path else None,
                        source_url=response.url if hasattr(response, "url") else None,
                        ingested_at=ingested_at,
                    ),
                    raw_path=raw_path,
                )

            results = quote_summary.get("result", [])
            if not results:
                return SourcePayload(
                    data=None,
                    status=SourceStatus(
                        source="analyst_targets",
                        success=False,
                        partial=True,
                        message="no analyst data available",
                        payload_path=str(raw_path) if raw_path else None,
                        source_url=response.url if hasattr(response, "url") else None,
                        ingested_at=ingested_at,
                    ),
                    raw_path=raw_path,
                )

            result = results[0]

            # --- financialData block ---
            financial_data = result.get("financialData", {})

            def _raw(obj: dict | None) -> float | None:
                if not obj:
                    return None
                val = obj.get("raw")
                if val is None:
                    return None
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return None

            target_mean = _raw(financial_data.get("targetMeanPrice"))
            target_high = _raw(financial_data.get("targetHighPrice"))
            target_low = _raw(financial_data.get("targetLowPrice"))
            target_median = _raw(financial_data.get("targetMedianPrice"))
            current_price = _raw(financial_data.get("currentPrice"))
            number_of_analysts_raw = financial_data.get("numberOfAnalystOpinions")
            number_of_analysts: int | None = None
            if number_of_analysts_raw:
                raw_val = number_of_analysts_raw.get("raw")
                if raw_val is not None:
                    try:
                        number_of_analysts = int(raw_val)
                    except (TypeError, ValueError):
                        number_of_analysts = None
            recommendation_key_raw = financial_data.get("recommendationKey")
            recommendation_key: str | None = None
            if isinstance(recommendation_key_raw, str) and recommendation_key_raw:
                recommendation_key = recommendation_key_raw
            recommendation_mean = _raw(financial_data.get("recommendationMean"))

            # Compute implied upside
            implied_upside: float | None = None
            if (
                target_mean is not None
                and current_price is not None
                and current_price != 0
            ):
                implied_upside = (target_mean - current_price) / current_price

            # --- recommendationTrend block ---
            recommendation_trend = result.get("recommendationTrend", {})
            trend_raw = recommendation_trend.get("trend", [])
            trend: list[dict] = []
            for entry in trend_raw:
                if isinstance(entry, dict):
                    trend.append(
                        {
                            "period": entry.get("period"),
                            "strongBuy": entry.get("strongBuy"),
                            "buy": entry.get("buy"),
                            "hold": entry.get("hold"),
                            "sell": entry.get("sell"),
                            "strongSell": entry.get("strongSell"),
                        }
                    )

            # --- upgradeDowngradeHistory block ---
            upgrade_history = result.get("upgradeDowngradeHistory", {})
            history_raw = upgrade_history.get("history", [])
            recent_changes: list[AnalystRatingChange] = []
            for item in history_raw[:12]:
                if not isinstance(item, dict):
                    continue
                epoch = item.get("epochGradeDate")
                change_date: date | None = None
                if epoch is not None:
                    try:
                        change_date = datetime.fromtimestamp(int(epoch), tz=timezone.utc).date()
                    except (TypeError, ValueError, OSError):
                        change_date = None
                recent_changes.append(
                    AnalystRatingChange(
                        firm=item.get("firm") or "",
                        change_date=change_date,
                        action=item.get("action") or "",
                        from_grade=item.get("fromGrade") or "",
                        to_grade=item.get("toGrade") or "",
                    )
                )

            snapshot = AnalystSnapshot(
                ticker=ticker,
                fetched_at=ingested_at,
                source="yahoo_quote_summary",
                target_mean=target_mean,
                target_high=target_high,
                target_low=target_low,
                target_median=target_median,
                current_price=current_price,
                implied_upside=implied_upside,
                number_of_analysts=number_of_analysts,
                recommendation_key=recommendation_key,
                recommendation_mean=recommendation_mean,
                trend=trend,
                recent_changes=recent_changes,
            )

            status = SourceStatus(
                source="analyst_targets",
                success=True,
                partial=False,
                message="ok",
                payload_path=str(raw_path) if raw_path else None,
                source_url=response.url if hasattr(response, "url") else None,
                ingested_at=ingested_at,
            )
            return SourcePayload(data=snapshot, status=status, raw_path=raw_path)

        except Exception as exc:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="analyst_targets",
                    success=False,
                    partial=True,
                    message=f"Failed to parse Yahoo Finance payload: {exc}",
                    payload_path=str(raw_path) if raw_path else None,
                    source_url=response.url if hasattr(response, "url") else None,
                    ingested_at=ingested_at,
                ),
                raw_path=raw_path,
            )

    # ------------------------------------------------------------------
    # Finnhub fallback path
    # ------------------------------------------------------------------

    def _try_finnhub(
        self, ticker: str, run_date: date, ingested_at: datetime, *, reason: str
    ) -> SourcePayload[AnalystSnapshot | None]:
        """Attempt Finnhub; return graceful failure if key is absent."""
        api_key = os.environ.get("FINNHUB_API_KEY")
        if not api_key:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="analyst_targets",
                    success=False,
                    partial=True,
                    message=(
                        f"FINNHUB_API_KEY unset; analyst-targets lane skipped "
                        f"(yahoo path skipped: {reason})"
                    ),
                    ingested_at=ingested_at,
                ),
            )
        return self._fetch_finnhub(ticker, run_date, ingested_at, api_key)

    def _fetch_finnhub(
        self, ticker: str, run_date: date, ingested_at: datetime, api_key: str
    ) -> SourcePayload[AnalystSnapshot | None]:
        """Fetch analyst targets from Finnhub three endpoints and merge."""
        # --- price target ---
        try:
            pt_response = self._http.get(
                self.finnhub_target_url,
                params={"symbol": ticker, "token": api_key},
            )
            pt_data = pt_response.json()
        except Exception as exc:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="analyst_targets",
                    success=False,
                    partial=True,
                    message=f"Finnhub price-target fetch failed: {exc}",
                    ingested_at=ingested_at,
                ),
            )

        # --- recommendation trend ---
        try:
            rec_response = self._http.get(
                self.finnhub_recommend_url,
                params={"symbol": ticker, "token": api_key},
            )
            rec_data = rec_response.json()
        except Exception:
            rec_data = []

        # --- upgrade/downgrade history ---
        try:
            upg_response = self._http.get(
                self.finnhub_upgrade_url,
                params={"symbol": ticker, "token": api_key},
            )
            upg_data = upg_response.json()
        except Exception:
            upg_data = []

        try:
            # Write raw payload to storage
            combined_raw = {
                "price_target": pt_data,
                "recommendation": rec_data,
                "upgrade_downgrade": upg_data,
            }
            try:
                raw_path = self._storage.write_raw_json(
                    run_date, "analyst_targets_finnhub", ticker.lower(), combined_raw
                )
            except Exception:
                raw_path = None

            # Parse price target fields
            target_mean: float | None = None
            target_high: float | None = None
            target_low: float | None = None
            target_median: float | None = None
            current_price: float | None = None
            number_of_analysts: int | None = None
            implied_upside: float | None = None

            if isinstance(pt_data, dict):
                def _f(val: object) -> float | None:
                    if val is None:
                        return None
                    try:
                        return float(val)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        return None

                target_mean = _f(pt_data.get("targetMean"))
                target_high = _f(pt_data.get("targetHigh"))
                target_low = _f(pt_data.get("targetLow"))
                target_median = _f(pt_data.get("targetMedian"))
                current_price = _f(pt_data.get("lastUpdated"))  # not available; skip
                # Finnhub doesn't return currentPrice directly — leave None
                current_price = None
                raw_analysts = pt_data.get("numberOfAnalysts")
                if raw_analysts is not None:
                    try:
                        number_of_analysts = int(raw_analysts)
                    except (TypeError, ValueError):
                        number_of_analysts = None

            # Recommendation key from latest trend entry
            recommendation_key: str | None = None
            recommendation_mean: float | None = None
            trend: list[dict] = []
            if isinstance(rec_data, list):
                for entry in rec_data[:4]:
                    if not isinstance(entry, dict):
                        continue
                    trend.append(
                        {
                            "period": entry.get("period"),
                            "strongBuy": entry.get("strongBuy"),
                            "buy": entry.get("buy"),
                            "hold": entry.get("hold"),
                            "sell": entry.get("sell"),
                            "strongSell": entry.get("strongSell"),
                        }
                    )

            # Upgrade/downgrade history
            recent_changes: list[AnalystRatingChange] = []
            if isinstance(upg_data, list):
                for item in upg_data[:12]:
                    if not isinstance(item, dict):
                        continue
                    change_date_str = item.get("gradeDate") or item.get("gradedate")
                    change_date_val: date | None = None
                    if change_date_str:
                        try:
                            change_date_val = date.fromisoformat(str(change_date_str)[:10])
                        except ValueError:
                            change_date_val = None
                    recent_changes.append(
                        AnalystRatingChange(
                            firm=item.get("company") or "",
                            change_date=change_date_val,
                            action=item.get("action") or "",
                            from_grade=item.get("fromGrade") or "",
                            to_grade=item.get("toGrade") or "",
                        )
                    )

            snapshot = AnalystSnapshot(
                ticker=ticker,
                fetched_at=ingested_at,
                source="finnhub",
                target_mean=target_mean,
                target_high=target_high,
                target_low=target_low,
                target_median=target_median,
                current_price=current_price,
                implied_upside=implied_upside,
                number_of_analysts=number_of_analysts,
                recommendation_key=recommendation_key,
                recommendation_mean=recommendation_mean,
                trend=trend,
                recent_changes=recent_changes,
            )

            return SourcePayload(
                data=snapshot,
                status=SourceStatus(
                    source="analyst_targets",
                    success=True,
                    partial=False,
                    message="ok (finnhub fallback)",
                    payload_path=str(raw_path) if raw_path else None,
                    ingested_at=ingested_at,
                ),
                raw_path=raw_path,
            )

        except Exception as exc:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="analyst_targets",
                    success=False,
                    partial=True,
                    message=f"Failed to parse Finnhub payload: {exc}",
                    ingested_at=ingested_at,
                ),
            )
