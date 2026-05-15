from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from market_sentiment.models import (
    FundamentalSnapshot,
    MacroObservation,
    OptionSnapshot,
    OfficialEvent,
    PipelineContext,
    PriceBar,
    ScoreCard,
)


def build_review_packet(
    generated_at: datetime,
    context: PipelineContext,
    scorecard: ScoreCard,
) -> dict[str, Any]:
    prices = sorted(context.prices, key=lambda bar: bar.trading_date)
    benchmark_prices = sorted(context.benchmark_prices, key=lambda bar: bar.trading_date)
    latest_price = prices[-1] if prices else None
    latest_benchmark = benchmark_prices[-1] if benchmark_prices else None
    macro_summary = _summarize_macro(context.macro)

    return _serialize(
        {
            "packet_type": "agent_review_packet",
            "schema_version": "1.2",
            "packet_id": f"{scorecard.run_date.isoformat()}::{scorecard.security.ticker}",
            "generated_at": generated_at,
            "run_date": scorecard.run_date,
            "data_quality": "insufficient" if scorecard.data_insufficient else "ok",
            "freshness": _build_freshness(scorecard, context),
            "security": asdict(scorecard.security),
            "benchmark_ticker": context.benchmark_ticker,
            "rule_engine_precheck": {
                "triggered": scorecard.triggered,
                "event_tag": scorecard.event_tag,
                "state": scorecard.state,
                "total_score": scorecard.total_score,
                "partial_coverage": scorecard.partial_coverage,
                "veto_reason": scorecard.veto_reason,
                "evidence": scorecard.evidence,
            },
            "trigger_summary": {
                "reasons": scorecard.trigger.reasons,
                "ten_day_drawdown": scorecard.trigger.ten_day_drawdown,
                "twenty_day_drawdown": scorecard.trigger.twenty_day_drawdown,
                "relative_underperformance": scorecard.trigger.relative_underperformance,
                "new_low": scorecard.trigger.new_low,
                "fresh_low_window": scorecard.trigger.fresh_low_window,
                "ten_day_window": _serialize(scorecard.trigger.ten_day_window),
                "twenty_day_window": _serialize(scorecard.trigger.twenty_day_window),
                "benchmark_twenty_day_window": _serialize(scorecard.trigger.benchmark_twenty_day_window),
            },
            "bucket_scores": {
                "fundamentals": asdict(scorecard.fundamentals),
                "sentiment": asdict(scorecard.sentiment),
                "social_rebound": asdict(scorecard.social_rebound),
                "chain_confirmation": asdict(scorecard.chain_confirmation),
                "price_flow": asdict(scorecard.price_flow),
                "risk_red_flags": asdict(scorecard.risk_red_flags),
            },
            "price_context": {
                "latest_security_bar": _serialize_price(latest_price),
                "latest_benchmark_bar": _serialize_price(latest_benchmark),
                "recent_security_bars": [_serialize_price(bar) for bar in prices[-90:]],
                "recent_benchmark_bars": [_serialize_price(bar) for bar in benchmark_prices[-90:]],
            },
            "official_events": [_serialize_event(event) for event in context.official_events[:5]],
            "fundamentals_snapshot": _serialize_fundamentals(context.fundamentals),
            "option_summary": _serialize_option_snapshot(getattr(context, "options_snapshot", None)),
            "social_summary": _serialize_social_summary(context),
            "macro_summary": macro_summary,
            "source_health": [_serialize(asdict(status)) for status in context.source_statuses],
            "social_source_health": [_serialize(asdict(status)) for status in getattr(context, "social_source_statuses", [])],
            "options_source_health": [_serialize(asdict(status)) for status in getattr(context, "options_source_statuses", [])],
            "decision_summary": {
                "top_positive_signals": _top_positive_signals(scorecard, context),
                "top_risk_signals": _top_risk_signals(scorecard, context),
                "next_checks": _next_checks(scorecard, context),
            },
            "agent_questions": [
                "这次下跌更像短期情绪波动、基本面恶化，还是系统性/行业性因素？",
                "当前证据是否支持继续观察、建立仓位，还是暂时回避？",
                "最重要的正面信号、负面信号、和接下来要跟踪的指标分别是什么？",
            ],
        }
    )



def _serialize_price(bar: PriceBar | None) -> dict[str, Any] | None:
    if bar is None:
        return None
    return _serialize(asdict(bar))


def _serialize_event(event: OfficialEvent) -> dict[str, Any]:
    return _serialize(asdict(event))


def _serialize_fundamentals(snapshot: FundamentalSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    revenue_growth = None
    if snapshot.revenue_latest is not None and snapshot.revenue_previous not in (None, 0):
        revenue_growth = (snapshot.revenue_latest - snapshot.revenue_previous) / abs(snapshot.revenue_previous)
    return _serialize(
        {
            **asdict(snapshot),
            "derived_metrics": {
                "revenue_growth": revenue_growth,
                "cash_minus_debt": (
                    snapshot.cash_latest - snapshot.debt_latest
                    if snapshot.cash_latest is not None and snapshot.debt_latest is not None
                    else None
                ),
            },
        }
    )


def _build_freshness(scorecard: ScoreCard, context: PipelineContext) -> dict[str, Any]:
    """Build freshness block to expose data ages explicitly to the agent."""
    run_date = scorecard.run_date
    fundamentals = context.fundamentals
    official_events = context.official_events

    # Compute fundamentals ages
    fundamentals_period_end = None
    fundamentals_filed_on = None
    fundamentals_age_days = None
    fundamentals_filed_age_days = None
    if fundamentals is not None:
        fundamentals_period_end = fundamentals.period_end
        fundamentals_filed_on = fundamentals.filed_on
        if fundamentals.period_end is not None:
            fundamentals_age_days = (run_date - fundamentals.period_end).days
        if fundamentals.filed_on is not None:
            fundamentals_filed_age_days = (run_date - fundamentals.filed_on).days

    # Compute official event ages
    latest_official_event_date = None
    official_event_age_days = None
    if official_events:
        latest_event = max(official_events, key=lambda e: e.event_time)
        latest_official_event_date = latest_event.event_time.date()
        official_event_age_days = (run_date - latest_official_event_date).days

    # Build caveats list
    caveats: list[str] = []

    # Caveat 1: Stale fundamentals (period_end > 100 days old)
    if fundamentals_age_days is not None and fundamentals_age_days > 100:
        period_end_iso = fundamentals_period_end.isoformat()
        caveats.append(
            f"fundamentals reflect period ending {period_end_iso}, ~{fundamentals_age_days} days old; "
            f"current quarter likely unreported"
        )
    # Caveat 2: Moderately stale fundamentals (60-100 days old)
    elif fundamentals_age_days is not None and 60 <= fundamentals_age_days <= 100:
        period_end_iso = fundamentals_period_end.isoformat()
        caveats.append(
            f"fundamentals reflect period ending {period_end_iso}, ~{fundamentals_age_days} days old; "
            f"verify whether next quarter has been reported"
        )
    # Caveat 3: No fundamentals snapshot
    if fundamentals is None:
        caveats.append("no fundamentals snapshot available for this ticker")

    # Caveat 4: Stale official events (> 30 days old)
    if official_event_age_days is not None and official_event_age_days > 30:
        date_iso = latest_official_event_date.isoformat()
        caveats.append(
            f"latest official filing was {official_event_age_days} days ago on {date_iso}; "
            f"no recent disclosures"
        )
    # Caveat 5: No official events
    if not official_events:
        caveats.append("no official events available for this ticker")

    return {
        "run_date": run_date.isoformat(),
        "fundamentals_period_end": fundamentals_period_end.isoformat() if fundamentals_period_end is not None else None,
        "fundamentals_filed_on": fundamentals_filed_on.isoformat() if fundamentals_filed_on is not None else None,
        "fundamentals_age_days": fundamentals_age_days,
        "fundamentals_filed_age_days": fundamentals_filed_age_days,
        "latest_official_event_date": latest_official_event_date.isoformat() if latest_official_event_date is not None else None,
        "official_event_age_days": official_event_age_days,
        "caveats": caveats,
    }


def _serialize_option_snapshot(snapshot: OptionSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return _serialize(asdict(snapshot))


def _summarize_macro(observations: list[MacroObservation]) -> dict[str, Any]:
    latest_by_name: dict[str, MacroObservation] = {}
    for observation in sorted(observations, key=lambda item: (item.name, item.observed_on)):
        latest_by_name[observation.name] = observation
    return {
        name: _serialize(asdict(observation))
        for name, observation in sorted(latest_by_name.items())
    }


def _serialize_social_summary(context: PipelineContext) -> dict[str, Any] | None:
    snapshot = getattr(context, "social_snapshot", None)
    if snapshot is None:
        return None
    return _serialize(
        {
            "state": snapshot.state,
            "score": snapshot.score,
            "recent_window_hours": snapshot.recent_window_hours,
            "baseline_days": snapshot.baseline_days,
            "provider_count": snapshot.provider_count,
            "community_count": snapshot.community_count,
            "total_posts": snapshot.total_posts,
            "informative_posts": snapshot.informative_posts,
            "recent_posts": snapshot.recent_posts,
            "baseline_posts": snapshot.baseline_posts,
            "unique_authors": snapshot.unique_authors,
            "author_concentration": snapshot.author_concentration,
            "recent_stance": snapshot.recent_stance,
            "baseline_stance": snapshot.baseline_stance,
            "delta": snapshot.delta,
            "breadth": snapshot.breadth,
            "hard_negative_ratio": snapshot.hard_negative_ratio,
            "provider_post_counts": snapshot.provider_post_counts,
            "recent_provider_post_counts": snapshot.recent_provider_post_counts,
            "source_brief": [
                _serialize_source_brief(source_summary)
                for source_summary in snapshot.source_summaries
            ],
            "source_breakdown": [
                {
                    "source": source_summary.source,
                    "total_posts": source_summary.total_posts,
                    "informative_posts": source_summary.informative_posts,
                    "recent_posts": source_summary.recent_posts,
                    "baseline_posts": source_summary.baseline_posts,
                    "unique_authors": source_summary.unique_authors,
                    "recent_stance": source_summary.recent_stance,
                    "baseline_stance": source_summary.baseline_stance,
                    "delta": source_summary.delta,
                    "hard_negative_ratio": source_summary.hard_negative_ratio,
                    "top_bullish_themes": [_serialize(asdict(theme)) for theme in source_summary.top_bullish_themes],
                    "top_bearish_themes": [_serialize(asdict(theme)) for theme in source_summary.top_bearish_themes],
                    "representative_posts": [
                        {
                            "source": post.source,
                            "community": post.community,
                            "created_at": post.created_at,
                            "title": post.title,
                            "url": post.url,
                            "engagement_score": post.engagement_score,
                            "themes": post.themes,
                        }
                        for post in source_summary.representative_posts
                    ],
                }
                for source_summary in snapshot.source_summaries
            ],
            "notes": snapshot.notes,
            "top_bullish_themes": [_serialize(asdict(theme)) for theme in snapshot.top_bullish_themes],
            "top_bearish_themes": [_serialize(asdict(theme)) for theme in snapshot.top_bearish_themes],
            "representative_posts": [
                {
                    "source": post.source,
                    "community": post.community,
                    "created_at": post.created_at,
                    "title": post.title,
                    "url": post.url,
                    "engagement_score": post.engagement_score,
                    "themes": post.themes,
                }
                for post in snapshot.representative_posts
            ],
        }
    )


def _serialize_source_brief(source_summary) -> dict[str, Any]:
    representative_posts = source_summary.representative_posts or []
    top_bullish = source_summary.top_bullish_themes[0].label if source_summary.top_bullish_themes else None
    top_bearish = source_summary.top_bearish_themes[0].label if source_summary.top_bearish_themes else None
    return _serialize(
        {
            "source": source_summary.source,
            "total_posts": source_summary.total_posts,
            "informative_posts": source_summary.informative_posts,
            "recent_posts": source_summary.recent_posts,
            "baseline_posts": source_summary.baseline_posts,
            "unique_authors": source_summary.unique_authors,
            "recent_stance": source_summary.recent_stance,
            "baseline_stance": source_summary.baseline_stance,
            "delta": source_summary.delta,
            "hard_negative_ratio": source_summary.hard_negative_ratio,
            "overall_tone": _classify_source_tone(source_summary.recent_stance, source_summary.delta),
            "top_bullish_theme": top_bullish,
            "top_bearish_theme": top_bearish,
            "representative_title": representative_posts[0].title if representative_posts else None,
        }
    )


def _classify_source_tone(recent_stance: float, delta: float) -> str:
    if recent_stance >= 0.20 and delta >= 0.10:
        return "positive_improving"
    if recent_stance <= -0.20 and delta <= -0.10:
        return "negative_worsening"
    if recent_stance >= 0.10:
        return "positive"
    if recent_stance <= -0.10:
        return "negative"
    return "mixed_unclear"


def _top_positive_signals(scorecard: ScoreCard, context: PipelineContext) -> list[str]:
    signals: list[str] = []
    snapshot = context.fundamentals
    if snapshot and snapshot.revenue_latest is not None and snapshot.revenue_previous not in (None, 0):
        revenue_growth = (snapshot.revenue_latest - snapshot.revenue_previous) / abs(snapshot.revenue_previous)
        if revenue_growth > 0:
            signals.append(f"营收同比增长 {revenue_growth:.1%}")
    if snapshot and snapshot.operating_cashflow_latest is not None and snapshot.operating_cashflow_latest > 0:
        signals.append("经营现金流为正")
    if snapshot and snapshot.cash_latest is not None and snapshot.debt_latest is not None and snapshot.cash_latest >= snapshot.debt_latest:
        signals.append("现金覆盖债务")
    if any(event.form_type in {"10-Q", "10-K", "8-K"} for event in context.official_events[:5]):
        signals.append("近期有高质量官方披露")
    social_snapshot = getattr(context, "social_snapshot", None)
    if social_snapshot and social_snapshot.score > 0:
        signals.append(f"社交情绪改善，delta {social_snapshot.delta:.2f}")
    options_snapshot = getattr(context, "options_snapshot", None)
    if options_snapshot and options_snapshot.put_call_volume_ratio is not None and options_snapshot.put_call_volume_ratio < 0.8:
        signals.append(f"期权成交量 put/call 比 {options_snapshot.put_call_volume_ratio:.2f}")
    if scorecard.event_tag.value == "company_specific":
        signals.append("更像公司特异性回撤")
    return signals[:3]


def _top_risk_signals(scorecard: ScoreCard, context: PipelineContext) -> list[str]:
    signals: list[str] = []
    snapshot = context.fundamentals
    if "fresh_low" in scorecard.trigger.reasons or scorecard.trigger.new_low:
        signals.append("价格仍在近期新低区间")
    if snapshot and snapshot.cash_latest is not None and snapshot.debt_latest is not None and snapshot.cash_latest < snapshot.debt_latest:
        signals.append("债务高于现金")
    if scorecard.partial_coverage:
        signals.append("本次数据覆盖不完整")
    social_snapshot = getattr(context, "social_snapshot", None)
    if social_snapshot and social_snapshot.score < 0:
        signals.append("社交讨论继续恶化")
    options_snapshot = getattr(context, "options_snapshot", None)
    if options_snapshot and options_snapshot.put_call_volume_ratio is not None and options_snapshot.put_call_volume_ratio >= 1.2:
        signals.append(f"期权成交量 put/call 比偏高 {options_snapshot.put_call_volume_ratio:.2f}")
    if scorecard.event_tag.value != "company_specific":
        signals.append("下跌可能受板块或市场共振影响")
    if not context.official_events:
        signals.append("近期缺少高质量官方披露")
    return signals[:3]


def _next_checks(scorecard: ScoreCard, context: PipelineContext) -> list[str]:
    checks: list[str] = []
    if "fresh_low" in scorecard.trigger.reasons or scorecard.trigger.new_low:
        checks.append("先观察是否脱离近期新低区间")
    checks.append(f"继续跟踪相对 {context.benchmark_ticker} 的强弱变化")
    if getattr(context, "options_snapshot", None):
        checks.append("继续跟踪最近到期日附近的 put/call 比和主力 strike")
    if any(event.form_type in {"10-Q", "10-K", "8-K"} for event in context.official_events[:5]):
        checks.append("跟踪下一次经营类披露是否延续当前结论")
    else:
        checks.append("等待更高质量的经营披露补充证据")
    return checks[:3]


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return _serialize(asdict(value))
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return value
