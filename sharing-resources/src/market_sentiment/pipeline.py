from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from market_sentiment.config import PRICE_HISTORY_TARGET_DAYS, ProjectConfig, load_config
from market_sentiment.decision_tracker import DecisionAlert, evaluate_decision, load_decision_files
from market_sentiment.http import HttpClient
from market_sentiment.models import (
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
from market_sentiment.review_packets import build_review_packet
from market_sentiment.runtime_preflight import PreflightSummary, build_preflight_summary
from market_sentiment.scoring import build_scorecard, classify_event_tag
from market_sentiment.valuation import compute_valuation_derived
from market_sentiment.social_service import SocialSignalService
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.analyst_targets import AnalystTargetsClient
from market_sentiment.sources.earnings_calendar import EarningsCalendarClient
from market_sentiment.sources.sec import SecClient
from market_sentiment.sources.stooq import StooqClient
from market_sentiment.sources.yahoo_finance import YahooFinanceClient
from market_sentiment.storage import Storage
from market_sentiment.triggers import compute_trigger

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
        (b) Evaluate active decisions against fresh market data.
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

            # (b) Evaluate active decisions.
            active_decisions = self.storage.read_active_decisions()
            for decision in active_decisions:
                ticker = decision["ticker"]
                decision_date = decision["decision_date"]

                # Fetch prices for this ticker.
                prices_payload, _ = self._fetch_prices_with_fallback(ticker, run_date)
                bars = self._filter_prices_as_of(prices_payload.data, run_date)

                # Fetch earnings if readily available.
                earnings = None
                try:
                    earnings_payload = self.earnings_calendar.fetch_next_earnings(ticker, run_date)
                    earnings = earnings_payload.data
                except Exception:
                    # Earnings is optional; pass None if fetch fails.
                    pass

                # Evaluate the decision.
                alert = evaluate_decision(decision, bars=bars, earnings=earnings, run_date=run_date)

                if alert:
                    # Decision fired; update its status.
                    alerts.append(alert)
                    self.storage.update_decision_status(
                        ticker=ticker,
                        decision_date=decision_date,
                        status=alert.kind,
                        status_reason=alert.reason,
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

    def run(self, run_date: date) -> DailyRunReport:
        self.storage.init_db()
        decision_time = datetime.now(timezone.utc)
        decision_tracking_result = self._track_decisions(run_date)
        benchmark_prices, benchmark_statuses, benchmark_status_map = self._fetch_benchmarks(run_date)
        macro_data, macro_statuses = self._fetch_macro(run_date)
        contexts: list[PipelineContext] = []
        all_statuses: list[SourceStatus] = benchmark_statuses.copy()
        review_packets: dict[str, dict] = {}

        for security in self.config.securities:
            security_prices, security_price_status, price_statuses = self.get_price_history(
                security.ticker, run_date
            )
            events_payload = self._fetch_events_with_recovery(security.ticker, run_date)
            companyfacts_payload = self._fetch_companyfacts_with_recovery(security.ticker, run_date)
            events = self._filter_events_as_of(events_payload.data, run_date)
            companyfacts = self._filter_fundamentals_as_of(companyfacts_payload.data, run_date)
            self.storage.upsert_events(events)
            self.storage.upsert_fundamentals(companyfacts)
            all_statuses.extend([*price_statuses, events_payload.status, companyfacts_payload.status])
            contexts.append(
                PipelineContext(
                    security=security,
                    benchmark_ticker=security.benchmark,
                    prices=security_prices,
                    benchmark_prices=benchmark_prices.get(security.benchmark, []),
                    official_events=events,
                    fundamentals=companyfacts,
                    macro=macro_data,
                    source_statuses=[
                        *benchmark_status_map.get(security.benchmark, []),
                        security_price_status,
                        events_payload.status,
                        companyfacts_payload.status,
                        *macro_statuses,
                    ],
                )
            )

        triggered_scorecards = []
        layer_peers = defaultdict(list)
        for context in contexts:
            layer_peers[context.security.layer].append(context)

        for context in contexts:
            threshold = self.config.thresholds[context.security.layer]
            trigger = compute_trigger(context.prices, context.benchmark_prices, threshold)
            if not trigger.triggered:
                continue
            social_collection = self.social.collect(context, run_date)
            context.social_source_statuses = social_collection.statuses
            context.social_snapshot = social_collection.snapshot
            context.social_posts_sample = social_collection.posts
            all_statuses.extend(social_collection.statuses)
            if self.config.options.enabled:
                options_payload = self._fetch_options_with_recovery(context.security.ticker, run_date)
                context.options_snapshot = options_payload.data
                context.options_source_statuses = [options_payload.status]
                all_statuses.append(options_payload.status)
            # Fetch next earnings date — this lane MUST NOT block the pipeline and MUST NOT mark partial_coverage
            # So we add the status to all_statuses for reporting, but NOT to context.source_statuses
            try:
                earnings_payload = self.earnings_calendar.fetch_next_earnings(context.security.ticker, run_date)
                context.earnings_calendar = earnings_payload.data
                all_statuses.append(earnings_payload.status)
            except Exception as exc:
                context.earnings_calendar = None
                failure_status = self._failure_status(
                    "yahoo_earnings_calendar", f"Failed to fetch earnings calendar for {context.security.ticker}: {exc}"
                )
                all_statuses.append(failure_status)
            # Fetch analyst price targets & rating momentum — Layer 2 advisory evidence ONLY.
            # MUST NOT block the pipeline, MUST NOT affect scores, MUST NOT mark partial_coverage.
            # Add status to all_statuses for reporting only, NOT to context.source_statuses.
            try:
                analyst_payload = self.analyst_targets.fetch_analyst_snapshot(context.security.ticker, run_date)
                context.analyst_snapshot = analyst_payload.data
                all_statuses.append(analyst_payload.status)
            except Exception as exc:
                context.analyst_snapshot = None
                failure_status = self._failure_status(
                    "analyst_targets", f"Failed to fetch analyst targets for {context.security.ticker}: {exc}"
                )
                all_statuses.append(failure_status)
            # Fetch richer SEC valuation fundamentals (reuses the companyfacts payload already
            # fetched above — see SecClient._get_companyfacts_payload) and compute derived
            # valuation inputs from it. Layer 2 advisory evidence ONLY: MUST NOT block the
            # pipeline, MUST NOT affect scores, MUST NOT mark partial_coverage.
            valuation_payload = self._fetch_valuation_fundamentals_with_recovery(context.security.ticker, run_date)
            context.valuation_fundamentals = valuation_payload.data
            all_statuses.append(valuation_payload.status)
            try:
                context.valuation_derived = compute_valuation_derived(
                    ticker=context.security.ticker,
                    as_of=run_date,
                    fundamentals=context.valuation_fundamentals,
                    prices=context.prices,
                    benchmark_prices=context.benchmark_prices,
                )
            except Exception as exc:
                context.valuation_derived = None
                all_statuses.append(
                    self._failure_status(
                        "valuation_derived", f"Failed to compute valuation derived metrics for {context.security.ticker}: {exc}"
                    )
                )
            peer_contexts = layer_peers[context.security.layer]
            event_tag = classify_event_tag(context, peer_contexts)
            scorecard = build_scorecard(
                run_date=run_date,
                context=context,
                trigger=trigger,
                event_tag=event_tag,
                peer_contexts=peer_contexts,
            )
            triggered_scorecards.append(scorecard)
            review_packets[context.security.ticker] = build_review_packet(
                decision_time, context, scorecard, peer_contexts=peer_contexts, storage=self.storage
            )

        report = DailyRunReport(
            run_date=run_date,
            generated_at=decision_time,
            triggered_count=sum(1 for scorecard in triggered_scorecards if scorecard.triggered),
            scorecards=sorted(triggered_scorecards, key=lambda item: item.total_score, reverse=True),
            source_statuses=dedupe_statuses(all_statuses + macro_statuses, decision_time),
        )
        self.storage.save_review_packets(run_date, review_packets)
        return report

    def _fetch_benchmarks(
        self,
        run_date: date,
    ) -> tuple[dict[str, list[PriceBar]], list[SourceStatus], dict[str, list[SourceStatus]]]:
        benchmark_prices: dict[str, list[PriceBar]] = {}
        statuses: list[SourceStatus] = []
        status_map: dict[str, list[SourceStatus]] = {}
        for benchmark in self.config.benchmarks.values():
            prices, payload_status, price_statuses = self.get_price_history(benchmark.ticker, run_date)
            benchmark_prices[benchmark.ticker] = prices
            statuses.extend(price_statuses)
            status_map[benchmark.ticker] = [payload_status]
        return benchmark_prices, statuses, status_map

    def _fetch_macro(self, run_date: date) -> tuple[list[MacroObservation], list[SourceStatus]]:
        observations: list[MacroObservation] = []
        statuses = []
        for name, series_id in self.config.fred_series.items():
            try:
                payload = self.fred.fetch_series(name, series_id, run_date)
            except Exception as exc:
                payload = SourcePayload(
                    data=[],
                    status=self._failure_status("fred", f"Failed to fetch FRED series {name}: {exc}"),
                )
            filtered = self._filter_macro_as_of(payload.data, run_date)
            self.storage.upsert_macro(filtered)
            observations.extend(filtered[-5:])
            statuses.append(payload.status)
        for name, series_id in self.config.eia_series.items():
            try:
                payload = self.eia.fetch_series(name, series_id, run_date)
            except Exception as exc:
                payload = SourcePayload(
                    data=[],
                    status=self._failure_status("eia", f"Failed to fetch EIA series {name}: {exc}"),
                )
            filtered = self._filter_macro_as_of(payload.data, run_date)
            self.storage.upsert_macro(filtered)
            observations.extend(filtered[-5:])
            statuses.append(payload.status)
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

        This is the single entry point ``run()``, ``_fetch_benchmarks``, and
        ``review_single`` use for price history: it backfills deeply exactly once per
        ticker (see ``_price_fetch_plan``), fetches only the incremental gap on every
        run after that, upserts whatever came back into the SQLite cache, and then
        reads the FULL merged history back out of the cache — so callers always get the
        deep multi-year series (needed for beta and own_history_percentile) regardless
        of how small this run's live fetch was, without re-downloading years of data on
        every run.

        Returns ``(merged_prices, winning_source_status, all_attempted_statuses)`` —
        ``winning_source_status`` is the single status of whichever source ultimately
        supplied this run's fresh bars (mirrors the pre-existing per-security
        ``source_statuses`` semantics used for ``partial_coverage``); the merged prices
        already reflect the deep cache regardless of which source won.
        """
        needs_deep, incremental_lookback_days = self._price_fetch_plan(ticker, run_date)
        lookback_days = PRICE_HISTORY_TARGET_DAYS if needs_deep else incremental_lookback_days

        payload, statuses = self._fetch_prices_with_fallback(
            ticker, run_date, lookback_days=lookback_days, deep=needs_deep
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
        deep: bool = False,
    ):
        """Try Tiger -> Yahoo -> Alpha Vantage -> Stooq -> SQLite cache, in order.

        ``lookback_days`` (Tiger/Yahoo) and ``deep`` (Alpha Vantage's full/compact
        toggle) let the caller ask for a shallow incremental top-up or a full
        multi-year backfill; omitting both preserves each source's own historical
        default window (unchanged behavior for any caller that doesn't need depth
        control). Stooq has no windowing parameter — it already serves whatever full
        history it has on every call, so it's left as-is.
        """
        try:
            primary = self.tiger.fetch_daily_prices(ticker, run_date, lookback_days=lookback_days)
        except Exception as exc:
            primary = SourcePayload(
                data=[],
                status=self._failure_status("tiger", f"Failed to fetch prices for {ticker}: {exc}"),
            )
        statuses = [primary.status]
        if primary.status.success and primary.data:
            return primary, statuses

        try:
            fallback_yahoo = self.yahoo.fetch_daily_prices(ticker, run_date, lookback_days=lookback_days)
        except Exception as exc:
            fallback_yahoo = SourcePayload(
                data=[],
                status=self._failure_status("yahoo_chart", f"Failed to fetch prices for {ticker}: {exc}"),
            )
        statuses.append(fallback_yahoo.status)
        if fallback_yahoo.status.success and fallback_yahoo.data:
            return fallback_yahoo, statuses

        try:
            fallback_av = self.alpha_vantage.fetch_daily_prices(ticker, run_date, deep=deep)
        except Exception as exc:
            fallback_av = SourcePayload(
                data=[],
                status=self._failure_status("alpha_vantage", f"Failed to fetch prices for {ticker}: {exc}"),
            )
        statuses.append(fallback_av.status)
        if fallback_av.status.success and fallback_av.data:
            return fallback_av, statuses

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

        # When Tiger, Yahoo, AV, and Stooq all have no data:
        cached = self.storage.read_cached_prices(
            ticker, days_back=PRICE_HISTORY_TARGET_DAYS, reference_date=run_date
        )
        if cached:
            latest = max(bar.trading_date for bar in cached)
            cache_status = SourceStatus(
                source="daily_prices_cache",
                success=True,
                partial=True,
                message=f"using cached prices through {latest.isoformat()}; Tiger+Yahoo+AV+Stooq all unavailable",
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

    def _fetch_options_with_recovery(self, ticker: str, run_date: date) -> SourcePayload[OptionSnapshot | None]:
        try:
            return self.options.fetch_realtime_chain(ticker, run_date)
        except Exception as exc:
            return SourcePayload(
                data=None,
                status=self._failure_status("alpha_vantage_options", f"Failed to fetch options for {ticker}: {exc}"),
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
        benchmark: str,
        layer: Layer,
        run_date: date,
        name: str | None = None,
    ) -> dict:
        """Run the price/SEC/macro/social lanes for one ad-hoc ticker and return a review packet dict.

        The ticker does not need to be in watchlist.toml.  This is the on-demand
        single-ticker path: pull the full evidence set for any ticker without
        waiting for a daily run.  A review packet is always returned even when
        ``compute_trigger`` reports ``triggered=False`` — the upstream triggering has
        already been done, and we need the full evidence set regardless.
        """
        self.storage.init_db()
        decision_time = datetime.now(timezone.utc)

        # Build a transient Security for this ad-hoc ticker.
        security = Security(
            ticker=ticker,
            name=name or ticker,
            layer=layer,
            benchmark=benchmark,
        )

        # --- Price lane (deep-backfilled once, then incremental — see get_price_history) ---
        prices, _, price_statuses = self.get_price_history(ticker, run_date)

        # --- Benchmark prices ---
        benchmark_prices, _, benchmark_price_statuses = self.get_price_history(benchmark, run_date)

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
            benchmark_ticker=benchmark,
            prices=prices,
            benchmark_prices=benchmark_prices,
            official_events=events,
            fundamentals=companyfacts,
            macro=macro_data,
            source_statuses=source_statuses,
        )

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

        # --- Options lane (only if enabled) ---
        if self.config.options.enabled:
            try:
                options_payload = self._fetch_options_with_recovery(ticker, run_date)
                context.options_snapshot = options_payload.data
                context.options_source_statuses = [options_payload.status]
            except Exception as exc:
                context.options_source_statuses = [
                    self._failure_status("alpha_vantage_options", f"Options lane failed for {ticker}: {exc}")
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
