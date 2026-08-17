from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from http.cookiejar import CookieJar
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener

from equity_research.http import HttpClient
from equity_research.models import (
    AnalystConsensusSnapshotRow,
    AnalystRatingActionRow,
    AnalystRatingChange,
    AnalystSnapshot,
    SourceStatus,
)
from equity_research.sources.base import SourcePayload
from equity_research.storage import Storage


# ---------------------------------------------------------------------------
# Yahoo cookie+crumb handshake
#
# ``quoteSummary`` is an UNDOCUMENTED, PRIVATE Yahoo Finance endpoint gated by an
# anti-automation "crumb" token. The handshake below (fc.yahoo.com for a session
# cookie, then /v1/test/getcrumb for the crumb) is reverse-engineered, not a
# published API contract. Yahoo can change or kill it without notice, at any time.
# For that reason every caller in this module treats it as inherently unreliable:
# failures here must degrade gracefully to Finnhub (or a clean no-op), never raise,
# never block the pipeline, and never affect scoring.
# ---------------------------------------------------------------------------

CrumbFetcher = Callable[[], "tuple[str, str] | None"]

_CRUMB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_CRUMB_COOKIE_URL = "https://fc.yahoo.com"
_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"


def fetch_yahoo_cookie_and_crumb(timeout: float = 10.0) -> tuple[str, str] | None:
    """Perform the real Yahoo cookie+crumb handshake. Returns ``(cookie_header, crumb)`` or None.

    Never raises. Two undocumented Yahoo requests, in order:

    1. ``GET https://fc.yahoo.com`` with a browser-like User-Agent, keeping cookies.
       The page itself returns an error — only its ``Set-Cookie`` header matters.
    2. ``GET https://query1.finance.yahoo.com/v1/test/getcrumb`` with those cookies,
       which returns a short crumb string that authorizes ``quoteSummary`` calls.
    """
    try:
        cookie_jar = CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookie_jar))
        try:
            opener.open(
                Request(_CRUMB_COOKIE_URL, headers={"User-Agent": _CRUMB_USER_AGENT}),
                timeout=timeout,
            ).close()
        except Exception:
            pass  # fc.yahoo.com deliberately serves an error page; only the cookie matters

        cookie_header = "; ".join(
            f"{cookie.name}={cookie.value}" for cookie in cookie_jar if cookie.value is not None
        )
        if not cookie_header:
            return None

        with opener.open(
            Request(_CRUMB_URL, headers={"User-Agent": _CRUMB_USER_AGENT, "Cookie": cookie_header}),
            timeout=timeout,
        ) as response:
            crumb = response.read().decode("utf-8", errors="ignore").strip().strip('"')
        if not crumb or "<" in crumb:
            return None
        return cookie_header, crumb
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pure helpers for the derived change-over-time signals (Part 3). Kept as
# free functions so they are directly unit-testable without any storage/HTTP
# plumbing.
# ---------------------------------------------------------------------------

def compute_pct_change(current: float | None, previous: float | None) -> tuple[float | None, str | None]:
    """(current - previous) / previous, or None with a reason."""
    if current is None or previous is None:
        return None, "missing value"
    if previous == 0:
        return None, "previous value is zero"
    return (current - previous) / previous, None


def compute_dispersion(
    target_high: float | None, target_low: float | None, target_mean: float | None
) -> tuple[float | None, str | None]:
    """(target_high - target_low) / target_mean, or None with a reason."""
    if target_high is None or target_low is None or target_mean is None:
        return None, "missing target_high/target_low/target_mean"
    if target_mean == 0:
        return None, "target_mean is zero"
    return (target_high - target_low) / target_mean, None


def find_snapshot_at_or_before(
    rows: list[AnalystConsensusSnapshotRow], cutoff: date
) -> AnalystConsensusSnapshotRow | None:
    """Latest row with run_date <= cutoff, or None. ``rows`` need not be sorted."""
    candidates = [row for row in rows if row.run_date <= cutoff]
    if not candidates:
        return None
    return max(candidates, key=lambda row: row.run_date)


def compute_days_since_changed(
    points: list[tuple[date, float | None]], run_date: date
) -> tuple[int | None, str | None]:
    """Days since the value at ``points[-1]`` (the current point) last differed.

    ``points`` must be sorted ascending by date with the current observation last.
    Walks backward while the value stays (approximately) equal to the current
    value; the returned count is the age of the earliest point still holding that
    value. If the value is stable across all stored history, this is a lower
    bound anchored at the earliest stored point, not proof it never changed before.
    """
    if len(points) < 2:
        return None, "no prior snapshot to compare"
    current_value = points[-1][1]
    if current_value is None:
        return None, "current value unavailable"
    last_stable_date = points[-1][0]
    for prior_date, prior_value in reversed(points[:-1]):
        if prior_value is None or abs(prior_value - current_value) > 1e-9:
            break
        last_stable_date = prior_date
    return (run_date - last_stable_date).days, None


def summarize_recent_actions(
    actions: list[AnalystRatingActionRow], run_date: date, window_days: int
) -> dict[str, Any]:
    """Counts of up/down/init actions and the acting firms within the trailing window."""
    cutoff = run_date - timedelta(days=window_days)
    windowed = [a for a in actions if cutoff <= a.action_date <= run_date]
    counts = {"up": 0, "down": 0, "init": 0}
    firms: set[str] = set()
    for action in windowed:
        if action.action in counts:
            counts[action.action] += 1
        if action.action in counts and action.firm:
            firms.add(action.firm)
    return {**counts, "firms": sorted(firms)}


class AnalystTargetsClient:
    """Fetch analyst price targets and rating momentum, and persist history for change-over-time signals.

    Primary source is Yahoo Finance ``quoteSummary`` (see the module-level cookie+crumb
    handshake docstring above — this is an undocumented private endpoint and WILL break
    without warning; every failure path here degrades gracefully). Falls back to Finnhub
    when Yahoo fails and ``FINNHUB_API_KEY`` is set.

    This is a Layer 2 advisory-evidence lane — it MUST NOT block the pipeline, MUST NOT
    affect scores, and MUST NOT mark partial_coverage. That discipline extends to the
    history bookkeeping added here: a storage failure while persisting the consensus
    snapshot or rating-action history must never turn an otherwise-successful fetch into
    a failure, and must never raise out of this class.
    """

    yahoo_base_url = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
    finnhub_target_url = "https://finnhub.io/api/v1/stock/price-target"
    finnhub_recommend_url = "https://finnhub.io/api/v1/stock/recommendation"
    finnhub_upgrade_url = "https://finnhub.io/api/v1/stock/upgrade-downgrade"

    # Lead/lag classification window: how many days before/after the latest up/down
    # rating action we look at price to decide whether the analyst led or followed.
    _LEAD_LAG_WINDOW_DAYS = 10
    _LEAD_LAG_MOVE_THRESHOLD = 0.03
    _DISPERSION_HIGH_THRESHOLD = 0.30
    _RECENT_ACTIONS_WINDOW_DAYS = 90

    def __init__(
        self,
        http_client: HttpClient,
        storage: Storage,
        *,
        crumb_fetcher: CrumbFetcher | None = None,
    ) -> None:
        self._http = http_client
        self._storage = storage
        # Injectable for tests; defaults to the real network handshake. Cached for the
        # process lifetime once obtained (see _ensure_crumb).
        self._crumb_fetcher: CrumbFetcher = crumb_fetcher or fetch_yahoo_cookie_and_crumb
        self._cached_cookie: str | None = None
        self._cached_crumb: str | None = None

    def fetch_analyst_snapshot(
        self, ticker: str, run_date: date
    ) -> SourcePayload[AnalystSnapshot | None]:
        """Fetch analyst price targets and rating momentum.

        Tries Yahoo Finance first; falls back to Finnhub when Yahoo fails and
        ``FINNHUB_API_KEY`` is set. Never raises — all exceptions are caught and
        returned as a failed ``SourcePayload``. On success, also persists a
        consensus snapshot and any new rating-action history rows, and attaches
        derived change-over-time signals to ``AnalystSnapshot.history_signals``;
        failures in that bookkeeping are swallowed and never affect the returned
        status.

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
    # Cookie+crumb handshake (cached for the process lifetime)
    # ------------------------------------------------------------------

    def _ensure_crumb(self, *, force_refresh: bool = False) -> tuple[str, str] | None:
        """Return a cached (or freshly fetched) (cookie_header, crumb) pair, or None."""
        if not force_refresh and self._cached_cookie is not None and self._cached_crumb is not None:
            return self._cached_cookie, self._cached_crumb
        try:
            result = self._crumb_fetcher()
        except Exception:
            result = None
        if not result:
            return None
        cookie_header, crumb = result
        if not cookie_header or not crumb:
            return None
        self._cached_cookie, self._cached_crumb = cookie_header, crumb
        return cookie_header, crumb

    # ------------------------------------------------------------------
    # Yahoo Finance path
    # ------------------------------------------------------------------

    def _fetch_yahoo(
        self, ticker: str, run_date: date, ingested_at: datetime
    ) -> SourcePayload[AnalystSnapshot | None]:
        encoded_ticker = quote(ticker, safe="")
        url = self.yahoo_base_url.format(ticker=encoded_ticker)

        credentials = self._ensure_crumb()
        if credentials is None:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="analyst_targets",
                    success=False,
                    partial=True,
                    message="Yahoo cookie/crumb handshake failed (undocumented endpoint may be unavailable)",
                    ingested_at=ingested_at,
                ),
            )

        response = None
        fetch_exc: Exception | None = None
        for attempt in range(2):
            cookie_header, crumb = credentials
            try:
                response = self._http.get(
                    url,
                    params={
                        "modules": "financialData,recommendationTrend,upgradeDowngradeHistory",
                        "crumb": crumb,
                    },
                    headers={"Cookie": cookie_header},
                )
                fetch_exc = None
                break
            except Exception as exc:
                fetch_exc = exc
                if attempt == 0 and "401" in str(exc):
                    credentials = self._ensure_crumb(force_refresh=True)
                    if credentials is None:
                        break
                    continue
                break

        if response is None:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="analyst_targets",
                    success=False,
                    partial=True,
                    message=f"Failed to fetch from Yahoo Finance: {fetch_exc}",
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
            # Parsed in full (not capped) so the full history can be persisted for
            # change-over-time signals; only a capped slice is kept on the snapshot
            # itself, matching prior display behaviour.
            upgrade_history = result.get("upgradeDowngradeHistory", {})
            history_raw = upgrade_history.get("history", [])
            all_changes = _parse_rating_changes(history_raw)
            recent_changes = all_changes[:12]

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

            self._finalize(ticker, run_date, snapshot, all_changes)

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

            # Upgrade/downgrade history — parsed in full for persistence, capped for display.
            all_changes: list[AnalystRatingChange] = []
            if isinstance(upg_data, list):
                for item in upg_data:
                    if not isinstance(item, dict):
                        continue
                    change_date_str = item.get("gradeDate") or item.get("gradedate")
                    change_date_val: date | None = None
                    if change_date_str:
                        try:
                            change_date_val = date.fromisoformat(str(change_date_str)[:10])
                        except ValueError:
                            change_date_val = None
                    all_changes.append(
                        AnalystRatingChange(
                            firm=item.get("company") or "",
                            change_date=change_date_val,
                            action=item.get("action") or "",
                            from_grade=item.get("fromGrade") or "",
                            to_grade=item.get("toGrade") or "",
                        )
                    )
            recent_changes = all_changes[:12]

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

            self._finalize(ticker, run_date, snapshot, all_changes)

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

    # ------------------------------------------------------------------
    # History persistence + derived signals (Layer 2 advisory evidence only —
    # see class docstring: must never raise, never affect scores/partial_coverage).
    # ------------------------------------------------------------------

    def _finalize(
        self,
        ticker: str,
        run_date: date,
        snapshot: AnalystSnapshot,
        all_changes: list[AnalystRatingChange],
    ) -> None:
        try:
            self._persist_consensus_snapshot(ticker, run_date, snapshot)
        except Exception:
            pass
        try:
            self._persist_rating_actions(ticker, all_changes, snapshot.fetched_at)
        except Exception:
            pass
        try:
            snapshot.history_signals = self._compute_history_signals(ticker, run_date, snapshot)
        except Exception:
            snapshot.history_signals = {
                "reasons": {"__all__": "history signal computation failed"}
            }

    def _persist_consensus_snapshot(
        self, ticker: str, run_date: date, snapshot: AnalystSnapshot
    ) -> None:
        price = self._storage.get_price_on_or_before(ticker, run_date)
        security_close = price[1] if price is not None else None
        row = AnalystConsensusSnapshotRow(
            ticker=ticker,
            run_date=run_date,
            target_mean=snapshot.target_mean,
            target_high=snapshot.target_high,
            target_low=snapshot.target_low,
            target_median=snapshot.target_median,
            number_of_analysts=snapshot.number_of_analysts,
            recommendation_key=snapshot.recommendation_key,
            recommendation_mean=snapshot.recommendation_mean,
            security_close=security_close,
            source=snapshot.source,
            ingested_at=snapshot.fetched_at,
        )
        self._storage.upsert_analyst_consensus_snapshot(row)

    def _persist_rating_actions(
        self, ticker: str, changes: list[AnalystRatingChange], ingested_at: datetime
    ) -> None:
        rows = [
            AnalystRatingActionRow(
                ticker=ticker,
                firm=change.firm,
                action_date=change.change_date,
                action=change.action,
                from_grade=change.from_grade,
                to_grade=change.to_grade,
                ingested_at=ingested_at,
            )
            for change in changes
            if change.change_date is not None and change.firm
        ]
        self._storage.upsert_analyst_rating_actions(rows)

    def _compute_history_signals(
        self, ticker: str, run_date: date, snapshot: AnalystSnapshot
    ) -> dict[str, Any]:
        history = self._storage.get_analyst_consensus_history(ticker)
        actions = self._storage.get_analyst_rating_actions(ticker)

        current_rows = [row for row in history if row.run_date == run_date]
        current_row = current_rows[-1] if current_rows else None
        prior_rows = [row for row in history if row.run_date < run_date]

        dispersion_value, dispersion_reason = compute_dispersion(
            snapshot.target_high, snapshot.target_low, snapshot.target_mean
        )
        dispersion_note = None
        if dispersion_value is not None and dispersion_value >= self._DISPERSION_HIGH_THRESHOLD:
            dispersion_note = (
                f"analysts disagree by {dispersion_value:.0%} of the target mean — "
                "the consensus mean alone carries limited information"
            )

        result: dict[str, Any] = {
            "days_since_last_snapshot": None,
            "days_since_target_changed": None,
            "target_change_pct": None,
            "price_change_pct": None,
            "target_change_pct_30d": None,
            "price_change_pct_30d": None,
            "target_change_pct_90d": None,
            "price_change_pct_90d": None,
            "dispersion": dispersion_value,
            "dispersion_note": dispersion_note,
            "recent_actions_90d": summarize_recent_actions(
                actions, run_date, self._RECENT_ACTIONS_WINDOW_DAYS
            ),
            "lead_lag": self._classify_lead_lag(ticker, run_date, actions),
        }
        reasons: dict[str, str] = {}
        if dispersion_reason:
            reasons["dispersion"] = dispersion_reason

        no_snapshot_fields = (
            "days_since_last_snapshot",
            "days_since_target_changed",
            "target_change_pct",
            "price_change_pct",
            "target_change_pct_30d",
            "price_change_pct_30d",
            "target_change_pct_90d",
            "price_change_pct_90d",
        )

        if current_row is None:
            for key in no_snapshot_fields:
                reasons[key] = "current run has no stored consensus snapshot"
            result["reasons"] = reasons
            return result

        if not prior_rows:
            for key in no_snapshot_fields:
                reasons[key] = (
                    "no prior consensus snapshot stored for this ticker — history starts today"
                )
            result["reasons"] = reasons
            return result

        previous_row = prior_rows[-1]
        result["days_since_last_snapshot"] = (run_date - previous_row.run_date).days

        points = [(row.run_date, row.target_mean) for row in prior_rows] + [
            (run_date, current_row.target_mean)
        ]
        days_changed, changed_reason = compute_days_since_changed(points, run_date)
        result["days_since_target_changed"] = days_changed
        if changed_reason:
            reasons["days_since_target_changed"] = changed_reason

        target_change, target_change_reason = compute_pct_change(
            current_row.target_mean, previous_row.target_mean
        )
        result["target_change_pct"] = target_change
        if target_change_reason:
            reasons["target_change_pct"] = target_change_reason

        price_change, price_change_reason = compute_pct_change(
            current_row.security_close, previous_row.security_close
        )
        result["price_change_pct"] = price_change
        if price_change_reason:
            reasons["price_change_pct"] = price_change_reason

        for label, days_back in (("30d", 30), ("90d", 90)):
            cutoff_row = find_snapshot_at_or_before(prior_rows, run_date - timedelta(days=days_back))
            if cutoff_row is None:
                reasons[f"target_change_pct_{label}"] = f"no consensus snapshot at/before {days_back} days ago"
                reasons[f"price_change_pct_{label}"] = reasons[f"target_change_pct_{label}"]
                continue
            t_change, t_reason = compute_pct_change(current_row.target_mean, cutoff_row.target_mean)
            p_change, p_reason = compute_pct_change(current_row.security_close, cutoff_row.security_close)
            result[f"target_change_pct_{label}"] = t_change
            result[f"price_change_pct_{label}"] = p_change
            if t_reason:
                reasons[f"target_change_pct_{label}"] = t_reason
            if p_reason:
                reasons[f"price_change_pct_{label}"] = p_reason

        result["reasons"] = reasons
        return result

    def _classify_lead_lag(
        self, ticker: str, run_date: date, actions: list[AnalystRatingActionRow]
    ) -> dict[str, Any]:
        """Classify whether the target moved before or after the price, using the most
        recent real (up/down) rating action as the anchor event.

        Conservative by design: anything short of a clearly distinguishable pre/post
        price reaction around that action is reported as ``unclear`` rather than guessed.
        Window and threshold used are always included so a reader can judge the evidence
        themselves.
        """
        base = {
            "window_days": self._LEAD_LAG_WINDOW_DAYS,
            "move_threshold_pct": self._LEAD_LAG_MOVE_THRESHOLD,
        }
        directional = [a for a in actions if a.action in ("up", "down")]
        if not directional:
            return {
                "classification": "unclear",
                "reason": "no up/down rating actions in stored history",
                "based_on_action": None,
                "price_change_pre_window_pct": None,
                "price_change_post_window_pct": None,
                **base,
            }

        latest = directional[-1]
        action_date = latest.action_date
        direction = 1 if latest.action == "up" else -1
        window = timedelta(days=self._LEAD_LAG_WINDOW_DAYS)
        based_on = {
            "firm": latest.firm,
            "date": action_date.isoformat(),
            "action": latest.action,
            "to_grade": latest.to_grade,
        }

        pre_anchor = self._storage.get_price_on_or_before(ticker, action_date - window)
        at_anchor = self._storage.get_price_on_or_before(ticker, action_date)
        if pre_anchor is None or at_anchor is None or pre_anchor[0] == at_anchor[0]:
            return {
                "classification": "unclear",
                "reason": f"insufficient price history around {action_date.isoformat()}",
                "based_on_action": based_on,
                "price_change_pre_window_pct": None,
                "price_change_post_window_pct": None,
                **base,
            }

        elapsed_since_action = (run_date - action_date).days
        pre_move = (at_anchor[1] - pre_anchor[1]) / pre_anchor[1] if pre_anchor[1] else None

        if elapsed_since_action < self._LEAD_LAG_WINDOW_DAYS:
            return {
                "classification": "unclear",
                "reason": (
                    f"only {elapsed_since_action}d elapsed since the latest rating action; "
                    f"{self._LEAD_LAG_WINDOW_DAYS}d needed to observe a post-action price reaction"
                ),
                "based_on_action": based_on,
                "price_change_pre_window_pct": pre_move,
                "price_change_post_window_pct": None,
                **base,
            }

        post_anchor = self._storage.get_price_on_or_before(ticker, action_date + window)
        post_move = None
        if post_anchor is not None and post_anchor[0] > at_anchor[0] and at_anchor[1]:
            post_move = (post_anchor[1] - at_anchor[1]) / at_anchor[1]

        def _significant(move: float | None) -> bool:
            return (
                move is not None
                and abs(move) >= self._LEAD_LAG_MOVE_THRESHOLD
                and (move > 0) == (direction > 0)
            )

        pre_significant = _significant(pre_move)
        post_significant = _significant(post_move)

        if post_significant and not pre_significant:
            classification = "analyst_led"
            reason = None
        elif pre_significant and not post_significant:
            classification = "analyst_followed"
            reason = None
        else:
            classification = "unclear"
            reason = "price move before and after the action were not clearly distinguishable"

        return {
            "classification": classification,
            "reason": reason,
            "based_on_action": based_on,
            "price_change_pre_window_pct": pre_move,
            "price_change_post_window_pct": post_move,
            **base,
        }


def _parse_rating_changes(history_raw: list) -> list[AnalystRatingChange]:
    """Parse the full (uncapped) upgradeDowngradeHistory list from Yahoo's payload."""
    changes: list[AnalystRatingChange] = []
    for item in history_raw:
        if not isinstance(item, dict):
            continue
        epoch = item.get("epochGradeDate")
        change_date: date | None = None
        if epoch is not None:
            try:
                change_date = datetime.fromtimestamp(int(epoch), tz=timezone.utc).date()
            except (TypeError, ValueError, OSError):
                change_date = None
        changes.append(
            AnalystRatingChange(
                firm=item.get("firm") or "",
                change_date=change_date,
                action=item.get("action") or "",
                from_grade=item.get("fromGrade") or "",
                to_grade=item.get("toGrade") or "",
            )
        )
    return changes
