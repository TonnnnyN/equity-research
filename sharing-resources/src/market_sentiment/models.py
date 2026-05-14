from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class Layer(StrEnum):
    AI_APPLICATIONS = "ai_applications"
    COMPUTE = "compute"
    UTILITIES = "utilities"


class EventTag(StrEnum):
    MARKET_WIDE = "market_wide"
    SECTOR_WIDE = "sector_wide"
    COMPANY_SPECIFIC = "company_specific"
    UNKNOWN = "unknown"


class ActionState(StrEnum):
    REJECT = "Reject"
    WATCH = "Watch"
    STARTER = "Starter"
    ADD = "Add"
    EXIT = "Exit"


class SocialReboundState(StrEnum):
    STRONG_REBOUND = "strong_rebound"
    MILD_REBOUND = "mild_rebound"
    FLAT = "flat_unclear"
    WORSENING = "worsening"
    INSUFFICIENT = "insufficient_data"


@dataclass(slots=True)
class Security:
    ticker: str
    name: str
    layer: Layer
    benchmark: str


@dataclass(slots=True)
class Benchmark:
    ticker: str
    name: str


@dataclass(slots=True)
class Threshold:
    ten_day_drawdown: float
    twenty_day_drawdown: float
    relative_underperformance: float
    new_low_window: int = 3


@dataclass(slots=True)
class PriceBar:
    ticker: str
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    source: str
    source_url: str | None = None
    ingested_at: datetime | None = None


@dataclass(slots=True)
class OfficialEvent:
    ticker: str
    event_time: datetime
    form_type: str
    title: str
    url: str
    source: str
    accepted_at: datetime | None = None
    ingested_at: datetime | None = None


@dataclass(slots=True)
class FundamentalSnapshot:
    ticker: str
    cik: str | None
    period_end: date | None
    filed_on: date | None
    revenue_latest: float | None
    revenue_previous: float | None
    operating_cashflow_latest: float | None
    operating_cashflow_previous: float | None
    capex_latest: float | None
    cash_latest: float | None
    debt_latest: float | None
    source: str
    source_url: str | None = None
    ingested_at: datetime | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MacroObservation:
    name: str
    observed_on: date
    value: float
    source: str
    source_url: str | None = None
    ingested_at: datetime | None = None


@dataclass(slots=True)
class SocialPost:
    ticker: str
    source: str
    community: str | None
    post_id: str
    created_at: datetime
    title: str
    body: str
    url: str
    author_handle: str | None = None
    author_id_hash: str | None = None
    engagement_score: float = 0.0
    comment_count: int | None = None
    like_count: int | None = None
    repost_count: int | None = None
    language: str | None = None
    is_repost: bool = False
    matched_text: bool = False
    stance: int | None = None
    quality_score: float | None = None
    themes: list[str] = field(default_factory=list)
    source_query: str | None = None
    source_url: str | None = None
    raw_payload_path: str | None = None
    ingested_at: datetime | None = None


@dataclass(slots=True)
class SocialThemeSummary:
    label: str
    direction: str
    count: int


@dataclass(slots=True)
class SocialSourceSummary:
    source: str
    total_posts: int
    informative_posts: int
    recent_posts: int
    baseline_posts: int
    unique_authors: int
    recent_stance: float
    baseline_stance: float
    delta: float
    hard_negative_ratio: float
    top_bullish_themes: list[SocialThemeSummary] = field(default_factory=list)
    top_bearish_themes: list[SocialThemeSummary] = field(default_factory=list)
    representative_posts: list[SocialPost] = field(default_factory=list)


@dataclass(slots=True)
class SocialSnapshot:
    ticker: str
    run_date: date
    recent_window_hours: int
    baseline_days: int
    provider_count: int
    community_count: int
    total_posts: int
    informative_posts: int
    recent_posts: int
    baseline_posts: int
    unique_authors: int
    author_concentration: float
    recent_stance: float
    baseline_stance: float
    delta: float
    breadth: float
    hard_negative_ratio: float
    state: SocialReboundState
    score: int
    provider_post_counts: dict[str, int] = field(default_factory=dict)
    recent_provider_post_counts: dict[str, int] = field(default_factory=dict)
    source_summaries: list[SocialSourceSummary] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    top_bullish_themes: list[SocialThemeSummary] = field(default_factory=list)
    top_bearish_themes: list[SocialThemeSummary] = field(default_factory=list)
    representative_posts: list[SocialPost] = field(default_factory=list)


@dataclass(slots=True)
class OptionContract:
    contract_id: str
    expiration: date
    strike: float
    option_type: str
    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: float | None = None
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    mark: float | None = None
    trade_date: date | None = None


@dataclass(slots=True)
class OptionSnapshot:
    ticker: str
    run_date: date
    source: str
    contract_count: int
    call_contracts: int
    put_contracts: int
    total_call_volume: int
    total_put_volume: int
    total_call_open_interest: int
    total_put_open_interest: int
    put_call_volume_ratio: float | None
    put_call_open_interest_ratio: float | None
    implied_volatility_avg: float | None = None
    nearest_expiration: date | None = None
    nearest_days_to_expiry: int | None = None
    max_call_open_interest_strike: float | None = None
    max_put_open_interest_strike: float | None = None
    top_contracts: list[OptionContract] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source_url: str | None = None
    raw_payload_path: str | None = None
    ingested_at: datetime | None = None


@dataclass(slots=True)
class SourceStatus:
    source: str
    success: bool
    partial: bool = False
    message: str = ""
    payload_path: str | None = None
    source_url: str | None = None
    ingested_at: datetime | None = None
    event_start: datetime | None = None
    event_end: datetime | None = None
    decision_time: datetime | None = None


@dataclass(slots=True)
class PriceWindow:
    start_date: date | None = None
    end_date: date | None = None
    start_close: float | None = None
    end_close: float | None = None
    drawdown: float | None = None


@dataclass(slots=True)
class TriggerResult:
    triggered: bool
    reasons: list[str] = field(default_factory=list)
    ten_day_drawdown: float | None = None
    twenty_day_drawdown: float | None = None
    relative_underperformance: float | None = None
    new_low: bool = False
    ten_day_window: PriceWindow | None = None
    twenty_day_window: PriceWindow | None = None
    benchmark_twenty_day_window: PriceWindow | None = None
    fresh_low_window: int | None = None


@dataclass(slots=True)
class BucketScore:
    name: str
    score: int
    max_score: int
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScoreCard:
    run_date: date
    security: Security
    event_tag: EventTag
    triggered: bool
    trigger: TriggerResult
    fundamentals: BucketScore
    sentiment: BucketScore
    chain_confirmation: BucketScore
    price_flow: BucketScore
    risk_red_flags: BucketScore
    total_score: int
    state: ActionState
    veto_reason: str | None = None
    partial_coverage: bool = False
    data_insufficient: bool = False
    evidence: list[str] = field(default_factory=list)
    social_rebound: BucketScore = field(default_factory=lambda: BucketScore("social_rebound", 0, 10, []))

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


@dataclass(slots=True)
class PipelineContext:
    security: Security
    benchmark_ticker: str
    prices: list[PriceBar]
    benchmark_prices: list[PriceBar]
    official_events: list[OfficialEvent]
    fundamentals: FundamentalSnapshot | None
    macro: list[MacroObservation]
    source_statuses: list[SourceStatus]
    social_snapshot: SocialSnapshot | None = None
    social_posts_sample: list[SocialPost] = field(default_factory=list)
    social_source_statuses: list[SourceStatus] = field(default_factory=list)
    options_snapshot: OptionSnapshot | None = None
    options_source_statuses: list[SourceStatus] = field(default_factory=list)


@dataclass(slots=True)
class DailyRunReport:
    run_date: date
    generated_at: datetime
    triggered_count: int
    scorecards: list[ScoreCard]
    source_statuses: list[SourceStatus]

    def to_dict(self) -> dict[str, Any]:
        return _serialize(
            {
                "run_date": self.run_date,
                "generated_at": self.generated_at,
                "triggered_count": self.triggered_count,
                "scorecards": [scorecard.to_dict() for scorecard in self.scorecards],
                "source_statuses": [asdict(status) for status in self.source_statuses],
            }
        )


@dataclass(slots=True)
class SocialPostCacheRow:
    source: str
    post_id: str
    ticker: str
    posted_at: datetime
    title: str
    sentiment: str
    confidence: float
    one_line_summary: str
    engagement_score: float
    ingested_at: datetime


@dataclass(slots=True)
class FilingSummaryCacheRow:
    cik: str
    accession_number: str
    ticker: str
    form_type: str
    filed_at: datetime
    summary: str
    sentiment: str
    key_metrics_json: str
    ingested_at: datetime
    period_end: datetime | None = None


def _serialize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return value
