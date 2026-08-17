from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from equity_research.config import PRICE_HISTORY_TARGET_DAYS, ProjectConfig, load_config
from equity_research.decision_tracker import DecisionAlert, evaluate_decision, evaluate_decision_through_date, load_decision_files
from equity_research.http import HttpClient
from equity_research.models import (
    DailyRunReport,
    FundamentalSnapshot,
    Layer,
    MacroObservation,
    OfficialEvent,
    PipelineContext,
    PriceBar,
    Security,
    SourceStatus,
    ValuationFundamentals,
)
from equity_research.review_packets import build_review_packet
from equity_research.runtime_preflight import PreflightSummary, build_preflight_summary
from equity_research.scoring import build_scorecard, classify_event_tag
from equity_research.valuation import compute_valuation_derived
from equity_research.social_service import SocialSignalService
from equity_research.sources.base import SourcePayload
from equity_research.sources.analyst_targets import AnalystTargetsClient
from equity_research.sources.earnings_calendar import EarningsCalendarClient
from equity_research.sources.sec import SecClient
from equity_research.sources.stooq import StooqClient
from equity_research.sources.yahoo_finance import YahooFinanceClient
from equity_research.storage import Storage
from equity_research.triggers import compute_trigger

# --- Price-history backfill/incremental policy -----------------------------------
#
# A ticker whose cached history's EARLIEST bar is already close to
# PRICE_HISTORY_TARGET_DAYS old is considered "deep enough" and only needs an
# incremental top-up from its LATEST cached bar forward. Anything shallower (no cache,
# a brand-new ticker, or a legacy pre-backfill database) triggers a one-time deep
# backfill request instead. See DailyPipeline._price_fetch_plan / get_price_history.

# How much slack to allow before re-triggering a deep backfill: a ticker backfilled
# `PRICE_HISTORY_TARGET_DAYS` deep N days ago will have an earliest-bar age of
# `PRICE_HISTORY_TARGET_DAYS - N` days from *today's* perspective as time moves on, so
# without slack it would look "shallow" again almost immediately. This buffer must stay
# well below the retention window's own buffer (see config.PRICE_HISTORY_TARGET_DAYS)
# so a ticker never gets pruned before it would naturally re-backfill.
_DEEP_BACKFILL_STALENESS_SLACK_DAYS = 60

# Bounds on the incremental fetch window (gap since the latest cached bar + slack for
# weekends/holidays/a skipped run). The minimum protects same-day/back-to-back runs;
# there is no need for an upper bound because a gap large enough to matter is exactly
# what re-triggers a deep backfill instead (see slack above).
_INCREMENTAL_LOOKBACK_SLACK_DAYS = 10
_INCREMENTAL_MIN_LOOKBACK_DAYS = 5


@dataclass
class DecisionTrackingResult:
    """Result of a full decision-tracking pass during the daily run."""

    ingest_warnings: list[str]
    alerts: list[DecisionAlert]
    active_summaries: list[dict]


class DailyPipeline:
    def __init__(self, config: ProjectConfig | None = None) -> None:
        self.config = config or load_config()
        self.storage = Storage(self.config.db_path, self.config.data_dir)
        user_agent = os.environ.get("SEC_USER_AGENT", self.config.default_user_agent)
        self.http = HttpClient(user_agent)
        self.sec = SecClient(self.http, self.storage)
        self.yahoo = YahooFinanceClient(self.http, self.storage)
        self.stooq = StooqClient(self.http, self.storage)
        self.earnings_calendar = EarningsCalendarClient(self.http, self.storage)
        self.analyst_targets = AnalystTargetsClient(self.http, self.storage)
        self.social = SocialSignalService(
            config=self.config,
            http=self.http,
            storage=self.storage,
        )

    def init_db(self) -> None:
        self.storage.init_db()

    def preflight(self) -> PreflightSummary:
        return build_preflight_summary(self.config)

    def _track_decisions(self, run_date: date) -> DecisionTrackingResult:
        """
        Execute a full decision-tracking pass:
        (a) Ingest new decision files from data/decisions/<date>/ subdirs.
        (b) Evaluate active decisions against fresh market data, covering all trading days
            since the decision was last checked (to catch conditions that fired during gaps).
        (c) Return a result with warnings, fired alerts, and summaries of still-active decisions.

        This method is defensive: any unhandled exception logs and returns an empty result,
        never breaking the main daily run.
        """
        ingest_warnings: list[str] = []
        alerts: list[DecisionAlert] = []
        active_summaries: list[dict] = []

        try:
            # (a) Ingest new decision files from data/decisions/
            decisions_dir = self.storage.data_dir / "decisions"
            if decisions_dir.exists():
                for subdir in sorted(decisions_dir.iterdir()):
                    if not subdir.is_dir():
                        continue

                    valid_payloads, warnings = load_decision_files(subdir)
                    ingest_warnings.extend(warnings)

                    for payload in valid_payloads:
                        ticker = payload.get("ticker", "?")
                        decision_date = payload.get("decision_date", "?")

                        # Skip if already in the table (don't resurrect invalidated decisions).
                        existing = self.storage.read_all_decisions_for_ticker(ticker)
                        if any(row["decision_date"] == decision_date for row in existing):
                            continue

                        self.storage.upsert_active_decision(
                            ticker=ticker,
                            decision_date=decision_date,
                            state=payload.get("state", ""),
                            reference_close=float(payload.get("reference_close", 0)),
                            invalidate_conditions=payload.get("invalidate_conditions", []),
                            rerate_conditions=payload.get("rerate_conditions", []),
                            status="active",
                        )

            # (b) Evaluate active decisions, covering all trading days since last check.
            active_decisions = self.storage.read_active_decisions()
            for decision in active_decisions:
                ticker = decision["ticker"]
                decision_date = decision["decision_date"]

                # Fetch prices for this ticker (full history).
                prices_payload, _ = self._fetch_prices_with_fallback(ticker, run_date)
                all_bars = self._filter_prices_as_of(prices_payload.data, run_date)

                if not all_bars:
                    continue

                # Determine the range of dates to check: from last_checked_date (or decision_date) to run_date
                last_checked = decision.get("last_checked_date")
                if last_checked:
                    check_from_date = date.fromisoformat(last_checked)
                else:
                    check_from_date = date.fromisoformat(decision_date)

                # Fetch earnings if readily available (use most recent)
                earnings = None
                try:
                    earnings_payload = self.earnings_calendar.fetch_next_earnings(ticker, run_date)
                    earnings = earnings_payload.data
                except Exception:
                    pass

                # Evaluate conditions day-by-day over the entire range since last check.
                # This catches conditions that fired during gaps and resolves on the first firing day.
                fired_alert, resolution_date, resolution_price = evaluate_decision_through_date(
                    decision,
                    bars=all_bars,
                    earnings=earnings,
                    since_date=check_from_date,
                    until_date=run_date,
                )

                if fired_alert:
                    # Decision fired; update its status with resolution price and date
                    alerts.append(fired_alert)
                    self.storage.update_decision_status(
                        ticker=ticker,
                        decision_date=decision_date,
                        status=fired_alert.kind,
                        status_reason=fired_alert.reason,
                        last_checked_date=resolution_date.isoformat(),
                        resolution_close=resolution_price,
                    )
                else:
                    # Check for expiry (90 calendar days from decision date)
                    days_held = (run_date - date.fromisoformat(decision_date)).days
                    if days_held > 90:
                        # Expired, close it
                        expiry_alert = DecisionAlert(
                            ticker=ticker,
                            decision_date=decision_date,
                            kind="expired",
                            reason=f"holding period elapsed ({days_held} calendar days)",
                            fired_condition=None,
                        )
                        alerts.append(expiry_alert)
                        self.storage.update_decision_status(
                            ticker=ticker,
                            decision_date=decision_date,
                            status="expired",
                            status_reason=expiry_alert.reason,
                            last_checked_date=run_date.isoformat(),
                        )
                    else:
                        # Decision still active; bump last_checked_date only.
                        self.storage.update_decision_status(
                            ticker=ticker,
                            decision_date=decision_date,
                            status="active",
                            status_reason=None,
                            last_checked_date=run_date.isoformat(),
                        )

                        # Build summary for still-active decision.
                        invalidate_conds = decision.get("invalidate_conditions", [])
                        rerate_conds = decision.get("rerate_conditions", [])

                        # Render conditions compactly for the report.
                        invalidate_strs = [
                            f"{cond.get('metric', '?')} {cond.get('comparator', '?')} {cond.get('threshold', '?')}"
                            for cond in invalidate_conds
                        ]
                        rerate_strs = [
                            f"{cond.get('metric', '?')} {cond.get('comparator', '?')} {cond.get('threshold', '?')}"
                            for cond in rerate_conds
                        ]

                        summary = {
                            "ticker": ticker,
                            "state": decision["state"],
                            "decision_date": decision_date,
                            "invalidate_conditions": invalidate_strs,
                            "rerate_conditions": rerate_strs,
                            "status": "未触发",
                        }
                        active_summaries.append(summary)

        except Exception as exc:
            # Log and return empty result if anything goes wrong; never break the pipeline.
            ingest_warnings.append(f"Decision tracking pass failed: {exc}")

        return DecisionTrackingResult(
            ingest_warnings=ingest_warnings,
            alerts=alerts,
            active_summaries=active_summaries,
        )

    def _fetch_macro(self, run_date: date) -> tuple[list[MacroObservation], list[SourceStatus]]:
        observations: list[MacroObservation] = []
        statuses = []
        return observations, statuses

    def _price_fetch_plan(self, ticker: str, run_date: date) -> tuple[bool, int]:
        """Decide whether ``ticker`` needs a deep backfill or just an incremental top-up.

        Returns ``(needs_deep, incremental_lookback_days)``. ``needs_deep`` is True
        when the cache has no data at all, or its EARLIEST bar isn't yet close to
        ``PRICE_HISTORY_TARGET_DAYS`` old (a brand-new ticker, or a legacy database from
        before this depth change). ``incremental_lookback_days`` is only meaningful when
        ``needs_deep`` is False: the gap since the cache's LATEST bar, plus slack for
        weekends/holidays/a skipped run, floored at a small minimum.
        """
        earliest = self.storage.get_earliest_cached_price_date(ticker)
        target_start = run_date - timedelta(days=PRICE_HISTORY_TARGET_DAYS)
        needs_deep = earliest is None or earliest > target_start + timedelta(
            days=_DEEP_BACKFILL_STALENESS_SLACK_DAYS
        )

        latest = self.storage.get_latest_cached_price_date(ticker)
        if latest is None:
            incremental_lookback_days = PRICE_HISTORY_TARGET_DAYS
        else:
            gap_days = max(0, (run_date - latest).days)
            incremental_lookback_days = max(
                _INCREMENTAL_MIN_LOOKBACK_DAYS, gap_days + _INCREMENTAL_LOOKBACK_SLACK_DAYS
            )
        return needs_deep, incremental_lookback_days

    def get_price_history(
        self, ticker: str, run_date: date
    ) -> tuple[list[PriceBar], SourceStatus, list[SourceStatus]]:
        """Fetch, cache, and return up to ``PRICE_HISTORY_TARGET_DAYS`` of daily prices.

        This is the single entry point ``review_single`` uses for price history:
        it backfills deeply exactly once per ticker (see ``_price_fetch_plan``), fetches
        only the incremental gap on every run after that, upserts whatever came back into
        the SQLite cache, and then reads the FULL merged history back out of the cache —
        so callers always get the deep multi-year series (needed for beta and
        own_history_percentile) regardless of how small this run's live fetch was, without
        re-downloading years of data on every run.

        Returns ``(merged_prices, winning_source_status, all_attempted_statuses)`` —
        ``winning_source_status`` is the single status of whichever source ultimately
        supplied this run's fresh bars (mirrors the pre-existing per-security
        ``source_statuses`` semantics used for ``partial_coverage``); the merged prices
        already reflect the deep cache regardless of which source won.
        """
        needs_deep, incremental_lookback_days = self._price_fetch_plan(ticker, run_date)
        lookback_days = PRICE_HISTORY_TARGET_DAYS if needs_deep else incremental_lookback_days

        payload, statuses = self._fetch_prices_with_fallback(
            ticker, run_date, lookback_days=lookback_days
        )
        fresh_bars = self._filter_prices_as_of(payload.data, run_date)
        self.storage.upsert_prices(fresh_bars)

        merged = self.storage.read_cached_prices(
            ticker, days_back=PRICE_HISTORY_TARGET_DAYS, reference_date=run_date
        )
        merged = self._filter_prices_as_of(merged, run_date)
        return merged, payload.status, statuses

    def _fetch_prices_with_fallback(
        self,
        ticker: str,
        run_date: date,
        *,
        lookback_days: int | None = None,
    ):
        """Try Yahoo -> Stooq -> SQLite cache, in order.

        ``lookback_days`` (Yahoo) lets the caller ask for a shallow incremental
        top-up or a full multi-year backfill; omitting it preserves Yahoo's default
        historical window. Stooq has no windowing parameter — it already serves
        whatever full history it has on every call, so it's left as-is.
        """
        try:
            primary = self.yahoo.fetch_daily_prices(ticker, run_date, lookback_days=lookback_days)
        except Exception as exc:
            primary = SourcePayload(
                data=[],
                status=self._failure_status("yahoo_chart", f"Failed to fetch prices for {ticker}: {exc}"),
            )
        statuses = [primary.status]
        if primary.status.success and primary.data:
            return primary, statuses

        try:
            fallback_stooq = self.stooq.fetch_daily_prices(ticker, run_date)
        except Exception as exc:
            try:
                fallback_stooq = self.stooq.fetch_daily_prices(ticker, run_date)
            except Exception as retry_exc:
                fallback_stooq = SourcePayload(
                    data=[],
                    status=self._failure_status("stooq", f"Failed to fetch fallback prices for {ticker}: {retry_exc}"),
                )
        statuses.append(fallback_stooq.status)
        if fallback_stooq.status.success and fallback_stooq.data:
            return fallback_stooq, statuses

        # When Yahoo and Stooq both have no data:
        cached = self.storage.read_cached_prices(
            ticker, days_back=PRICE_HISTORY_TARGET_DAYS, reference_date=run_date
        )
        if cached:
            latest = max(bar.trading_date for bar in cached)
            cache_status = SourceStatus(
                source="daily_prices_cache",
                success=True,
                partial=True,
                message=f"using cached prices through {latest.isoformat()}; Yahoo+Stooq all unavailable",
                source_url=None,
                ingested_at=datetime.now(timezone.utc),
            )
            statuses.append(cache_status)
            return SourcePayload(data=cached, status=cache_status), statuses

        return primary, statuses

    def _fetch_events_with_recovery(self, ticker: str, run_date: date) -> SourcePayload[list[OfficialEvent]]:
        try:
            return self.sec.fetch_recent_events(ticker, run_date)
        except Exception as exc:
            return SourcePayload(
                data=[],
                status=self._failure_status("sec", f"Failed to fetch SEC events for {ticker}: {exc}"),
            )

    def _fetch_companyfacts_with_recovery(
        self,
        ticker: str,
        run_date: date,
    ) -> SourcePayload[FundamentalSnapshot | None]:
        try:
            return self.sec.fetch_company_facts(ticker, run_date)
        except Exception as exc:
            return SourcePayload(
                data=None,
                status=self._failure_status("sec_companyfacts", f"Failed to fetch SEC companyfacts for {ticker}: {exc}"),
            )

    def _fetch_valuation_fundamentals_with_recovery(
        self,
        ticker: str,
        run_date: date,
    ) -> SourcePayload[ValuationFundamentals | None]:
        try:
            return self.sec.fetch_valuation_fundamentals(ticker, run_date)
        except Exception as exc:
            return SourcePayload(
                data=None,
                status=self._failure_status(
                    "sec_valuation_fundamentals", f"Failed to fetch SEC valuation fundamentals for {ticker}: {exc}"
                ),
            )


    @staticmethod
    def _filter_prices_as_of(prices: list[PriceBar], run_date: date) -> list[PriceBar]:
        return [bar for bar in prices if bar.trading_date <= run_date]

    @staticmethod
    def _filter_events_as_of(events: list[OfficialEvent], run_date: date) -> list[OfficialEvent]:
        return [event for event in events if event.event_time.date() <= run_date]

    @staticmethod
    def _filter_macro_as_of(observations: list[MacroObservation], run_date: date) -> list[MacroObservation]:
        return [observation for observation in observations if observation.observed_on <= run_date]

    @staticmethod
    def _filter_fundamentals_as_of(
        snapshot: FundamentalSnapshot | None,
        run_date: date,
    ) -> FundamentalSnapshot | None:
        if snapshot is None:
            return None
        dated_fields = [value for value in (snapshot.period_end, snapshot.filed_on) if value is not None]
        if any(value > run_date for value in dated_fields):
            return None
        return snapshot

    def review_single(
        self,
        ticker: str,
        *,
        benchmark: str | None,
        layer: Layer | None,
        run_date: date,
        name: str | None = None,
    ) -> dict:
        """Run the price/SEC/macro/social lanes for one ad-hoc ticker and return a review packet dict.

        This is the on-demand single-ticker path: pull the full evidence set for any ticker.
        A review packet is always returned regardless of trigger state — the metrics include
        trigger information even when triggered=False, so all tickers get complete analysis.
        """
        self.storage.init_db()
        decision_time = datetime.now(timezone.utc)

        # Resolve benchmark: use provided value or default to QQQ
        resolved_benchmark = benchmark if benchmark is not None else "QQQ"
        benchmark_was_specified = benchmark is not None

        # Build a transient Security for this ad-hoc ticker.
        security = Security(
            ticker=ticker,
            name=name or ticker,
            layer=layer,
            benchmark=resolved_benchmark,
        )

        # --- Price lane (deep-backfilled once, then incremental — see get_price_history) ---
        prices, _, price_statuses = self.get_price_history(ticker, run_date)

        # --- Benchmark prices ---
        benchmark_prices, _, benchmark_price_statuses = self.get_price_history(resolved_benchmark, run_date)

        # --- SEC events lane ---
        events_payload = self._fetch_events_with_recovery(ticker, run_date)
        events = self._filter_events_as_of(events_payload.data, run_date)

        # --- SEC companyfacts lane ---
        companyfacts_payload = self._fetch_companyfacts_with_recovery(ticker, run_date)
        companyfacts = self._filter_fundamentals_as_of(companyfacts_payload.data, run_date)

        # --- Macro lane ---
        macro_data, macro_statuses = self._fetch_macro(run_date)

        source_statuses = [
            *benchmark_price_statuses,
            *price_statuses,
            events_payload.status,
            companyfacts_payload.status,
            *macro_statuses,
        ]

        context = PipelineContext(
            security=security,
            benchmark_ticker=resolved_benchmark,
            prices=prices,
            benchmark_prices=benchmark_prices,
            official_events=events,
            fundamentals=companyfacts,
            macro=macro_data,
            source_statuses=source_statuses,
        )
        # Store layer specification info for later inclusion in the packet
        context.layer_was_specified = layer is not None
        context.benchmark_was_specified = benchmark_was_specified

        # --- Social lane (always attempted, graceful if disabled or fails) ---
        try:
            social_collection = self.social.collect(context, run_date)
            context.social_source_statuses = social_collection.statuses
            context.social_snapshot = social_collection.snapshot
            context.social_posts_sample = social_collection.posts
        except Exception as exc:
            context.social_source_statuses = [
                self._failure_status("social", f"Social lane failed for {ticker}: {exc}")
            ]

        # --- Earnings calendar (non-blocking) ---
        try:
            earnings_payload = self.earnings_calendar.fetch_next_earnings(ticker, run_date)
            context.earnings_calendar = earnings_payload.data
        except Exception:
            context.earnings_calendar = None

        # --- Analyst price targets & rating momentum (non-blocking, Layer 2 advisory only) ---
        try:
            analyst_payload = self.analyst_targets.fetch_analyst_snapshot(ticker, run_date)
            context.analyst_snapshot = analyst_payload.data
        except Exception:
            context.analyst_snapshot = None

        # --- SEC valuation fundamentals (non-blocking, Layer 2 advisory only) ---
        valuation_payload = self._fetch_valuation_fundamentals_with_recovery(ticker, run_date)
        context.valuation_fundamentals = valuation_payload.data
        try:
            context.valuation_derived = compute_valuation_derived(
                ticker=ticker,
                as_of=run_date,
                fundamentals=context.valuation_fundamentals,
                prices=context.prices,
                benchmark_prices=context.benchmark_prices,
            )
        except Exception:
            context.valuation_derived = None

        # --- Trigger / scorecard ---
        threshold = self.config.thresholds.get(layer)
        if threshold is None:
            # Fall back to first available threshold when the layer has no config entry.
            threshold = next(iter(self.config.thresholds.values()))

        trigger = compute_trigger(context.prices, context.benchmark_prices, threshold)
        event_tag = classify_event_tag(context, [])
        scorecard = build_scorecard(
            run_date=run_date,
            context=context,
            trigger=trigger,
            event_tag=event_tag,
            peer_contexts=[],
        )

        # Build and return packet regardless of triggered state.
        return build_review_packet(decision_time, context, scorecard, storage=self.storage)

    @staticmethod
    def _failure_status(source: str, message: str) -> SourceStatus:
        return SourceStatus(
            source=source,
            success=False,
            partial=True,
            message=message,
            ingested_at=datetime.now(timezone.utc),
        )


def dedupe_statuses(statuses: list[SourceStatus], decision_time: datetime) -> list[SourceStatus]:
    deduped = {}
    for status in statuses:
        status.decision_time = decision_time
        deduped[(status.source, status.payload_path, status.message)] = status
    return list(deduped.values())
