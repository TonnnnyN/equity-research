from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime, timezone

from market_sentiment.config import ProjectConfig, load_config
from market_sentiment.http import HttpClient
from market_sentiment.manual_agent_report import render_manual_agent_report
from market_sentiment.models import (
    DailyRunReport,
    FundamentalSnapshot,
    MacroObservation,
    OptionSnapshot,
    OfficialEvent,
    PipelineContext,
    PriceBar,
    SourceStatus,
)
from market_sentiment.review_packets import build_review_packet
from market_sentiment.runtime_preflight import PreflightSummary, build_preflight_summary
from market_sentiment.scoring import build_scorecard, classify_event_tag
from market_sentiment.social_service import SocialSignalService
from market_sentiment.subagent_sentiment import build_default_sentiment_judge
from market_sentiment.sources.alpha_vantage import AlphaVantageClient
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.eia import EiaClient
from market_sentiment.sources.options_alpha_vantage import AlphaVantageOptionsClient
from market_sentiment.sources.fred import FredClient
from market_sentiment.sources.sec import SecClient
from market_sentiment.sources.stooq import StooqClient
from market_sentiment.sources.tiger import TigerClient
from market_sentiment.sources.yahoo_finance import YahooFinanceClient
from market_sentiment.storage import Storage
from market_sentiment.triggers import compute_trigger


class DailyPipeline:
    def __init__(self, config: ProjectConfig | None = None) -> None:
        self.config = config or load_config()
        self.storage = Storage(self.config.db_path, self.config.data_dir)
        user_agent = os.environ.get("SEC_USER_AGENT", self.config.default_user_agent)
        self.http = HttpClient(user_agent)
        self.sec = SecClient(self.http, self.storage)
        self.tiger = TigerClient(self.http, self.storage)
        self.yahoo = YahooFinanceClient(self.http, self.storage)
        self.alpha_vantage = AlphaVantageClient(self.http, self.storage)
        self.stooq = StooqClient(self.http, self.storage)
        self.fred = FredClient(self.http, self.storage)
        self.eia = EiaClient(self.http, self.storage)
        self.options = AlphaVantageOptionsClient(self.http, self.storage, self.config.options)
        self.social = SocialSignalService(
            config=self.config,
            http=self.http,
            storage=self.storage,
            sentiment_judge=build_default_sentiment_judge(),
        )

    def init_db(self) -> None:
        self.storage.init_db()

    def preflight(self) -> PreflightSummary:
        return build_preflight_summary(self.config)

    def run(self, run_date: date) -> DailyRunReport:
        self.storage.init_db()
        decision_time = datetime.now(timezone.utc)
        benchmark_prices, benchmark_statuses, benchmark_status_map = self._fetch_benchmarks(run_date)
        macro_data, macro_statuses = self._fetch_macro(run_date)
        contexts: list[PipelineContext] = []
        all_statuses: list[SourceStatus] = benchmark_statuses.copy()
        review_packets: dict[str, dict] = {}

        for security in self.config.securities:
            security_prices_payload, price_statuses = self._fetch_prices_with_fallback(security.ticker, run_date)
            events_payload = self._fetch_events_with_recovery(security.ticker, run_date)
            companyfacts_payload = self._fetch_companyfacts_with_recovery(security.ticker, run_date)
            security_prices = self._filter_prices_as_of(security_prices_payload.data, run_date)
            events = self._filter_events_as_of(events_payload.data, run_date)
            companyfacts = self._filter_fundamentals_as_of(companyfacts_payload.data, run_date)
            self.storage.upsert_prices(security_prices)
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
                        security_prices_payload.status,
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
            review_packets[context.security.ticker] = build_review_packet(decision_time, context, scorecard)

        report = DailyRunReport(
            run_date=run_date,
            generated_at=decision_time,
            triggered_count=sum(1 for scorecard in triggered_scorecards if scorecard.triggered),
            scorecards=sorted(triggered_scorecards, key=lambda item: item.total_score, reverse=True),
            source_statuses=dedupe_statuses(all_statuses + macro_statuses, decision_time),
        )
        self.storage.save_review_packets(run_date, review_packets)
        self.storage.save_manual_agent_report(run_date, render_manual_agent_report(report, review_packets))
        return report

    def _fetch_benchmarks(
        self,
        run_date: date,
    ) -> tuple[dict[str, list[PriceBar]], list[SourceStatus], dict[str, list[SourceStatus]]]:
        benchmark_prices: dict[str, list[PriceBar]] = {}
        statuses: list[SourceStatus] = []
        status_map: dict[str, list[SourceStatus]] = {}
        for benchmark in self.config.benchmarks.values():
            payload, price_statuses = self._fetch_prices_with_fallback(benchmark.ticker, run_date)
            prices = self._filter_prices_as_of(payload.data, run_date)
            self.storage.upsert_prices(prices)
            benchmark_prices[benchmark.ticker] = prices
            statuses.extend(price_statuses)
            status_map[benchmark.ticker] = [payload.status]
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

    def _fetch_prices_with_fallback(self, ticker: str, run_date: date):
        try:
            primary = self.tiger.fetch_daily_prices(ticker, run_date)
        except Exception as exc:
            primary = SourcePayload(
                data=[],
                status=self._failure_status("tiger", f"Failed to fetch prices for {ticker}: {exc}"),
            )
        statuses = [primary.status]
        if primary.status.success and primary.data:
            return primary, statuses

        try:
            fallback_yahoo = self.yahoo.fetch_daily_prices(ticker, run_date)
        except Exception as exc:
            fallback_yahoo = SourcePayload(
                data=[],
                status=self._failure_status("yahoo_chart", f"Failed to fetch prices for {ticker}: {exc}"),
            )
        statuses.append(fallback_yahoo.status)
        if fallback_yahoo.status.success and fallback_yahoo.data:
            return fallback_yahoo, statuses

        try:
            fallback_av = self.alpha_vantage.fetch_daily_prices(ticker, run_date)
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
        cached = self.storage.read_cached_prices(ticker, days_back=60)
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
