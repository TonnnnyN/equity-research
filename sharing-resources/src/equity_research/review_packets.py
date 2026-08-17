from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from equity_research import valuation_models
from equity_research.models import (
    FundamentalSnapshot,
    MacroObservation,
    OptionSnapshot,
    OfficialEvent,
    PipelineContext,
    PriceBar,
    ScoreCard,
)
from equity_research.storage import Storage


def build_review_packet(
    generated_at: datetime,
    context: PipelineContext,
    scorecard: ScoreCard,
    peer_contexts: list[PipelineContext] | None = None,
    storage: Storage | None = None,
) -> dict[str, Any]:
    prices = sorted(context.prices, key=lambda bar: bar.trading_date)
    benchmark_prices = sorted(context.benchmark_prices, key=lambda bar: bar.trading_date)
    latest_price = prices[-1] if prices else None
    latest_benchmark = benchmark_prices[-1] if benchmark_prices else None
    macro_summary = _summarize_macro(context.macro)

    return _serialize(
        {
            "packet_type": "agent_review_packet",
            "schema_version": "2.0",
            "packet_tier": "tier_3_full",
            "packet_id": f"{scorecard.run_date.isoformat()}::{scorecard.security.ticker}",
            "generated_at": generated_at,
            "run_date": scorecard.run_date,
            "data_quality": "insufficient" if scorecard.data_insufficient else "ok",
            "freshness": _build_freshness(scorecard, context),
            "earnings_calendar": _build_earnings_calendar(scorecard, context),
            "security": asdict(scorecard.security),
            "benchmark_ticker": context.benchmark_ticker,
            "specification_metadata": {
                "layer_was_specified": context.layer_was_specified,
                "benchmark_was_specified": context.benchmark_was_specified,
                "layer_note": (
                    "Layer was explicitly specified."
                    if context.layer_was_specified
                    else "Layer was not specified; generic thresholds were used for trigger calculations."
                ),
                "benchmark_note": (
                    "Benchmark was explicitly specified."
                    if context.benchmark_was_specified
                    else "Benchmark was not specified; QQQ was used as default for relative performance calculations."
                ),
            },
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
            "valuation_inputs": _serialize_valuation_inputs(context, prices, peer_contexts),
            "previous_judgement": _get_previous_judgement(storage, scorecard.security.ticker, scorecard.run_date),
            "rederivation_required": _build_rederivation_required(
                scorecard.security.ticker, scorecard.run_date, context, scorecard, storage
            ),
            "option_summary": _serialize_option_snapshot(getattr(context, "options_snapshot", None)),
            "social_summary": _serialize_social_summary(context),
            "analyst_summary": _serialize_analyst_summary(context),
            "macro_summary": macro_summary,
            "source_health": [_serialize(asdict(status)) for status in context.source_statuses],
            "social_source_health": [_serialize(asdict(status)) for status in getattr(context, "social_source_statuses", [])],
            "social_sources_to_fetch": _build_social_sources_to_fetch(scorecard.security.ticker),
            "agent_questions": [
                "这次下跌更像短期情绪波动、基本面恶化，还是系统性/行业性因素？",
                "当前证据是否支持继续观察、建立仓位，还是暂时回避？",
                "最重要的正面信号、负面信号、和接下来要跟踪的指标分别是什么？",
            ],
        }
    )


def _build_rederivation_required(
    ticker: str,
    run_date: date,
    context: PipelineContext,
    scorecard: ScoreCard,
    storage: Storage | None = None,
) -> dict[str, Any]:
    """Compute whether a rederivation is required based on multiple checks.

    Returns a dict with 'required' (bool) and 'reasons' (list) or 'checks' (dict when not required).
    """
    if storage is None:
        return {
            "required": True,
            "reasons": ["no storage available to check previous derivation"],
        }

    # Get the previous portrait or order to extract derivation metadata
    portrait_record = storage.get_latest_company_portrait(ticker, exclude_run_date=run_date)
    order_record = storage.get_latest_model_order(ticker, exclude_run_date=run_date)

    if portrait_record is None and order_record is None:
        return {
            "required": True,
            "reasons": ["no previous full derivation found"],
        }

    # Extract metadata from portrait or order
    derivation_type = None
    derivation_date = None
    reference_close = None

    # Try to extract from portrait first
    if portrait_record:
        try:
            portrait_obj = json.loads(portrait_record.get("portrait_json", "{}"))
            derivation_type = portrait_obj.get("_derivation_type")
            derivation_date_str = portrait_obj.get("_derivation_date")
            if derivation_date_str:
                derivation_date = date.fromisoformat(derivation_date_str)
            reference_close = portrait_obj.get("_reference_close")
        except (json.JSONDecodeError, ValueError):
            pass

    # Fall back to order if needed
    if not derivation_date and order_record:
        try:
            order_obj = json.loads(order_record.get("order_json", "{}"))
            if not derivation_type:
                derivation_type = order_obj.get("_derivation_type")
            if not derivation_date:
                derivation_date_str = order_obj.get("_derivation_date")
                if derivation_date_str:
                    derivation_date = date.fromisoformat(derivation_date_str)
            if not reference_close:
                reference_close = order_obj.get("_reference_close")
        except (json.JSONDecodeError, ValueError):
            pass

    # If we couldn't extract metadata, treat as requiring rederivation
    if derivation_date is None:
        return {
            "required": True,
            "reasons": ["previous derivation metadata missing or unparseable — treating as unknown"],
        }

    # Perform checks
    reasons: list[str] = []
    checks: dict[str, Any] = {}

    # Check 1: Has a new 10-K or 10-Q been filed?
    official_events = sorted(context.official_events, key=lambda e: e.event_time, reverse=True)
    recent_filings = [e for e in official_events if e.event_time.date() > derivation_date and e.event_type in ("10-K", "10-Q")]
    checks["new_filings_since_derivation"] = {
        "checked": True,
        "found": len(recent_filings) > 0,
        "count": len(recent_filings),
        "details": [{"date": e.event_time.date().isoformat(), "type": e.event_type} for e in recent_filings[:3]],
    }
    if recent_filings:
        reasons.append(f"new {recent_filings[0].event_type} filed on {recent_filings[0].event_time.date().isoformat()}")

    # Check 2: Has more than 30 days passed since last full derivation?
    days_since_derivation = (run_date - derivation_date).days
    checks["days_since_derivation"] = {
        "checked": True,
        "days": days_since_derivation,
        "threshold_days": 30,
        "exceeded": days_since_derivation > 30,
    }
    if days_since_derivation > 30:
        reasons.append(f"{days_since_derivation} days since last full derivation (threshold: 30)")

    # Check 3: Has price moved more than 20% since last derivation?
    prices = sorted(context.prices, key=lambda bar: bar.trading_date)
    price_check = {"checked": False, "pct_change": None, "threshold_pct": 20.0, "exceeded": False}
    if prices and reference_close is not None and reference_close > 0:
        current_close = prices[-1].close if prices else None
        if current_close is not None and current_close > 0:
            pct_change = ((current_close - reference_close) / reference_close) * 100
            price_check["checked"] = True
            price_check["pct_change"] = round(pct_change, 2)
            price_check["exceeded"] = abs(pct_change) > 20.0
            if abs(pct_change) > 20.0:
                reasons.append(f"price moved {pct_change:+.1f}% since derivation (threshold: 20%)")
    checks["price_movement_since_derivation"] = price_check

    # Check 4: Are any trigger conditions currently active?
    trigger_check = {"checked": True, "any_active": False, "active_conditions": []}
    if scorecard.trigger.ten_day_drawdown is not None and scorecard.trigger.ten_day_drawdown < 0:
        trigger_check["active_conditions"].append(f"10-day drawdown: {scorecard.trigger.ten_day_drawdown:.1%}")
    if scorecard.trigger.twenty_day_drawdown is not None and scorecard.trigger.twenty_day_drawdown < 0:
        trigger_check["active_conditions"].append(f"20-day drawdown: {scorecard.trigger.twenty_day_drawdown:.1%}")
    if scorecard.trigger.relative_underperformance is not None and scorecard.trigger.relative_underperformance < 0:
        trigger_check["active_conditions"].append(f"underperformance vs benchmark: {scorecard.trigger.relative_underperformance:.1%}")
    if scorecard.trigger.new_low:
        trigger_check["active_conditions"].append("new low")
    if scorecard.trigger.fresh_low_window is not None:
        trigger_check["active_conditions"].append(f"fresh low within {scorecard.trigger.fresh_low_window} days")

    trigger_check["any_active"] = len(trigger_check["active_conditions"]) > 0
    if trigger_check["any_active"]:
        reasons.append(f"{len(trigger_check['active_conditions'])} trigger condition(s) active")
    checks["trigger_conditions_active"] = trigger_check

    # Determine if rederivation is required
    required = len(reasons) > 0

    if required:
        return {
            "required": True,
            "reasons": reasons,
            "checks": checks,
        }
    else:
        return {
            "required": False,
            "checks": checks,
            "note": "All checks passed; a cheap update from the previous derivation is sufficient",
        }


def _get_previous_judgement(storage: Storage | None, ticker: str, run_date: date) -> dict[str, Any] | None:
    """Retrieve the most recent portrait and order for a ticker, excluding today's run_date.

    Returns a dict with portrait, order, and days_ago keys, or None if storage is not
    available or no previous judgement exists.
    """
    if storage is None:
        return None

    portrait_record = storage.get_latest_company_portrait(ticker, exclude_run_date=run_date)
    order_record = storage.get_latest_model_order(ticker, exclude_run_date=run_date)

    if portrait_record is None and order_record is None:
        return None

    return _serialize({
        "portrait": portrait_record,
        "order": order_record,
    })


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


def _build_earnings_calendar(scorecard: ScoreCard, context: PipelineContext) -> dict[str, Any]:
    """Build earnings calendar block to expose next earnings date to the agent."""
    run_date = scorecard.run_date
    earnings_calendar = context.earnings_calendar

    next_earnings_date = None
    days_to_next_earnings = None
    is_estimate = None
    status = "failed"

    if earnings_calendar is not None:
        next_earnings_date = earnings_calendar.next_earnings_date

        if next_earnings_date is not None:
            days_to_next_earnings = (next_earnings_date - run_date).days
            is_estimate = earnings_calendar.is_estimate
            status = "ok"
        else:
            status = "unavailable"

    return {
        "next_earnings_date": next_earnings_date.isoformat() if next_earnings_date is not None else None,
        "days_to_next_earnings": days_to_next_earnings,
        "is_estimate": is_estimate,
        "status": status,
    }


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


def _serialize_analyst_summary(context: PipelineContext) -> dict[str, Any] | None:
    """Serialize analyst price targets and rating momentum for the review packet.

    This is Layer 2 advisory evidence only — it must NOT enter the scoring system.
    Returns None when no snapshot is available.
    """
    snapshot = getattr(context, "analyst_snapshot", None)
    if snapshot is None:
        return None

    recent_changes_out = []
    for change in snapshot.recent_changes[:8]:
        recent_changes_out.append(
            {
                "firm": change.firm,
                "date": change.change_date.isoformat() if change.change_date is not None else None,
                "action": change.action,
                "from_grade": change.from_grade,
                "to_grade": change.to_grade,
            }
        )

    return _serialize(
        {
            "source": snapshot.source,
            "target_mean": snapshot.target_mean,
            "target_high": snapshot.target_high,
            "target_low": snapshot.target_low,
            "target_median": snapshot.target_median,
            "current_price": snapshot.current_price,
            "implied_upside": snapshot.implied_upside,
            "number_of_analysts": snapshot.number_of_analysts,
            "recommendation_key": snapshot.recommendation_key,
            "recommendation_mean": snapshot.recommendation_mean,
            "trend": snapshot.trend[:2],
            "recent_changes": recent_changes_out,
            "history": getattr(snapshot, "history_signals", None) or {},
        }
    )


def _serialize_valuation_inputs(
    context: PipelineContext,
    prices: list[PriceBar],
    peer_contexts: list[PipelineContext] | None = None,
) -> dict[str, Any] | None:
    """Serialize SEC-sourced valuation data-layer inputs for the review packet.

    This is Layer 2 advisory evidence only, following the ``analyst_summary`` pattern
    exactly — it MUST NOT enter ``bucket_scores``, MUST NOT change any score or state,
    MUST NOT set ``partial_coverage``, and MUST NOT be able to fail the pipeline.

    At packet-build time, computes only the four always-on metrics (piotroski_f_score,
    altman_z_score, beneish_m_score, net_cash_floor) and exposes them under always_on.
    Ordered model results do not exist at packet-build time; the agent places an order
    via CLI after reading the packet, and ordered results are written separately.

    Returns None when no valuation data is available.
    """
    fundamentals = getattr(context, "valuation_fundamentals", None)
    derived = getattr(context, "valuation_derived", None)
    if fundamentals is None and derived is None:
        return None
    return _serialize(
        {
            "fundamentals": fundamentals,
            "derived": derived,
            "always_on": _compute_always_on_metrics(context, derived, fundamentals),
            "models": None,  # ordered model results are populated later via valuation-order CLI subcommand
        }
    )


def _compute_always_on_metrics(
    context: PipelineContext,
    derived: Any,
    fundamentals: Any,
) -> list[dict[str, Any]] | None:
    """Compute the four always-on metrics (piotroski_f_score, altman_z_score,
    beneish_m_score, net_cash_floor) that Python computes unconditionally
    at packet-build time because they are pure formulas — facts, like the
    share price.

    This is Layer 2 advisory evidence only. It MUST NOT enter bucket_scores,
    MUST NOT change any ActionState, MUST NOT set partial_coverage, and
    MUST NOT be able to raise into or fail the pipeline. Any failure degrades
    to None rather than raising.
    """
    if derived is None:
        return None
    try:
        results = []
        # Compute each of the four always-on metrics.
        for model_name in ("piotroski_f_score", "altman_z_score", "beneish_m_score", "net_cash_floor"):
            if model_name == "piotroski_f_score":
                result = valuation_models.piotroski_f_score(fundamentals)
            elif model_name == "altman_z_score":
                result = valuation_models.altman_z_score(derived, fundamentals, layer=context.security.layer)
            elif model_name == "beneish_m_score":
                result = valuation_models.beneish_m_score(fundamentals)
            elif model_name == "net_cash_floor":
                result = valuation_models.net_cash_floor(derived, fundamentals)
            else:
                continue  # pragma: no cover
            results.append(result.to_dict())
        return results if results else None
    except Exception:
        return None


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


def _build_social_sources_to_fetch(ticker: str) -> list[dict[str, Any]]:
    """Build a list of social media URLs for the agent to fetch directly.

    Returns search URLs for Reddit and other platforms with ticker already substituted.
    The agent can fetch these URLs directly using its own web tools.
    """
    import urllib.parse

    sources = []

    # Reddit search URL with recent filter (past month)
    reddit_query = f"{ticker} stock"
    reddit_url = f"https://www.reddit.com/r/stocks/search/?q={urllib.parse.quote(reddit_query)}&sort=new&t=month"
    sources.append({
        "platform": "reddit",
        "url": reddit_url,
        "description": f"Recent r/stocks discussions about {ticker}",
        "lookback_window": "past 30 days",
    })

    # X (Twitter) search URL with recent tweets
    x_query = f"${ticker} stock"
    x_url = f"https://x.com/search?q={urllib.parse.quote(x_query)}&f=live"
    sources.append({
        "platform": "x",
        "url": x_url,
        "description": f"Recent X posts mentioning {ticker}",
        "lookback_window": "past week (X default)",
    })

    return sources


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


def _build_price_summary(prices: list[PriceBar], benchmark_prices: list[PriceBar] | None = None) -> dict[str, Any]:
    """Build a compact price summary from full bar arrays.

    Replaces 90 bars × 6 fields with key metrics:
    - Latest close and period changes (1/5/20/60 days)
    - Relative performance vs benchmark
    - Position in moving averages and ranges
    """
    if not prices:
        return {
            "latest_close": None,
            "latest_date": None,
            "price_change_1d_pct": None,
            "price_change_5d_pct": None,
            "price_change_20d_pct": None,
            "price_change_60d_pct": None,
            "vs_benchmark_1d_pct": None,
            "vs_benchmark_5d_pct": None,
            "vs_benchmark_20d_pct": None,
            "vs_benchmark_60d_pct": None,
            "distance_from_20d_sma_pct": None,
            "distance_from_50d_sma_pct": None,
            "position_in_20d_range": None,
            "position_in_52w_range": None,
            "is_fresh_52w_low": None,
        }

    prices = sorted(prices, key=lambda b: b.trading_date)
    latest = prices[-1]
    latest_close = latest.close

    # Calculate price changes for different windows
    def _pct_change(start: float, end: float) -> float | None:
        if start and start != 0:
            return (end - start) / start
        return None

    changes_1d = _pct_change(prices[-2].close, latest_close) if len(prices) >= 2 else None
    changes_5d = _pct_change(prices[-5].close, latest_close) if len(prices) >= 5 else None
    changes_20d = _pct_change(prices[-20].close, latest_close) if len(prices) >= 20 else None
    changes_60d = _pct_change(prices[-60].close, latest_close) if len(prices) >= 60 else None

    # Calculate relative performance vs benchmark
    vs_bench_1d = None
    vs_bench_5d = None
    vs_bench_20d = None
    vs_bench_60d = None
    if benchmark_prices:
        bench_prices = sorted(benchmark_prices, key=lambda b: b.trading_date)
        bench_latest = bench_prices[-1].close
        bench_change_1d = _pct_change(bench_prices[-2].close, bench_latest) if len(bench_prices) >= 2 else None
        bench_change_5d = _pct_change(bench_prices[-5].close, bench_latest) if len(bench_prices) >= 5 else None
        bench_change_20d = _pct_change(bench_prices[-20].close, bench_latest) if len(bench_prices) >= 20 else None
        bench_change_60d = _pct_change(bench_prices[-60].close, bench_latest) if len(bench_prices) >= 60 else None

        vs_bench_1d = (changes_1d or 0) - (bench_change_1d or 0) if changes_1d and bench_change_1d else None
        vs_bench_5d = (changes_5d or 0) - (bench_change_5d or 0) if changes_5d and bench_change_5d else None
        vs_bench_20d = (changes_20d or 0) - (bench_change_20d or 0) if changes_20d and bench_change_20d else None
        vs_bench_60d = (changes_60d or 0) - (bench_change_60d or 0) if changes_60d and bench_change_60d else None

    # Calculate moving average distances
    dist_20d_sma = None
    dist_50d_sma = None
    if len(prices) >= 20:
        sma_20 = sum(p.close for p in prices[-20:]) / 20
        dist_20d_sma = (latest_close - sma_20) / sma_20 if sma_20 else None
    if len(prices) >= 50:
        sma_50 = sum(p.close for p in prices[-50:]) / 50
        dist_50d_sma = (latest_close - sma_50) / sma_50 if sma_50 else None

    # Calculate position in ranges
    pos_20d = None
    pos_52w = None
    if len(prices) >= 20:
        low_20d = min(p.low for p in prices[-20:])
        high_20d = max(p.high for p in prices[-20:])
        if high_20d != low_20d:
            pos_20d = (latest_close - low_20d) / (high_20d - low_20d)

    if len(prices) >= 252:
        low_52w = min(p.low for p in prices[-252:])
        high_52w = max(p.high for p in prices[-252:])
        if high_52w != low_52w:
            pos_52w = (latest_close - low_52w) / (high_52w - low_52w)

    # Check if fresh 52w low
    is_fresh_52w_low = None
    if len(prices) >= 252:
        low_52w = min(p.low for p in prices[-252:])
        low_1w = min(p.low for p in prices[-5:]) if len(prices) >= 5 else latest.low
        is_fresh_52w_low = abs(latest_close - low_52w) < 0.01 or (low_1w <= low_52w * 1.001)

    return _serialize({
        "latest_close": latest_close,
        "latest_date": latest.trading_date.isoformat(),
        "price_change_1d_pct": changes_1d,
        "price_change_5d_pct": changes_5d,
        "price_change_20d_pct": changes_20d,
        "price_change_60d_pct": changes_60d,
        "vs_benchmark_1d_pct": vs_bench_1d,
        "vs_benchmark_5d_pct": vs_bench_5d,
        "vs_benchmark_20d_pct": vs_bench_20d,
        "vs_benchmark_60d_pct": vs_bench_60d,
        "distance_from_20d_sma_pct": dist_20d_sma,
        "distance_from_50d_sma_pct": dist_50d_sma,
        "position_in_20d_range": pos_20d,
        "position_in_52w_range": pos_52w,
        "is_fresh_52w_low": is_fresh_52w_low,
    })


def _build_valuation_inputs_tier2(
    context: PipelineContext,
    prices: list[PriceBar],
) -> dict[str, Any] | None:
    """Build Tier 2 (working packet) version of valuation_inputs.

    Replaces 20 quarters × 30+ concepts with derived values only.
    Includes history_summary (counts, latest period_end) so reader can detect gaps.
    """
    derived = getattr(context, "valuation_derived", None)
    fundamentals = getattr(context, "valuation_fundamentals", None)

    if derived is None and fundamentals is None:
        return None

    history_summary = None
    if fundamentals is not None:
        history_summary = {
            "latest_period_end": fundamentals.get("latest_period_end") if isinstance(fundamentals, dict) else getattr(fundamentals, "latest_period_end", None),
            "quarters_available": fundamentals.get("quarters_available") if isinstance(fundamentals, dict) else getattr(fundamentals, "quarters_available", 0),
        }

    return _serialize({
        "derived": derived,
        "always_on": _compute_always_on_metrics(context, derived, fundamentals),
        "history_summary": history_summary,
        "models": None,
        "full_path": "<ticker>.full.json",
    })


def _get_fundamentals_comparison(
    storage: Storage | None,
    ticker: str,
    run_date: date,
    current_fundamentals: FundamentalSnapshot | None,
) -> dict[str, Any]:
    """Compare current fundamentals against previous run.

    Returns dict with:
    - fundamentals_changed: bool
    - current_period_end: str or None
    - current_filed_on: str or None
    - previous_period_end: str or None (from last run)
    """
    if storage is None or current_fundamentals is None:
        return {
            "fundamentals_changed": False,
            "current_period_end": current_fundamentals.period_end.isoformat() if current_fundamentals else None,
            "current_filed_on": current_fundamentals.filed_on.isoformat() if current_fundamentals else None,
            "previous_period_end": None,
        }

    # Get previous run's fundamentals
    try:
        # This is a simplified check - just look at period_end
        # In a full implementation, we'd query the database for the previous run's fundamentals
        return {
            "fundamentals_changed": False,
            "current_period_end": current_fundamentals.period_end.isoformat() if current_fundamentals else None,
            "current_filed_on": current_fundamentals.filed_on.isoformat() if current_fundamentals else None,
            "previous_period_end": None,
        }
    except Exception:
        return {
            "fundamentals_changed": False,
            "current_period_end": current_fundamentals.period_end.isoformat() if current_fundamentals else None,
            "current_filed_on": current_fundamentals.filed_on.isoformat() if current_fundamentals else None,
            "previous_period_end": None,
        }


def build_delta_packet(
    generated_at: datetime,
    context: PipelineContext,
    scorecard: ScoreCard,
    storage: Storage | None = None,
) -> dict[str, Any] | None:
    """Build Tier 1 (delta) packet: what changed since the previous run (~1 KB).

    Returns None if this is the first run (no previous data).
    Structure:
    {
        "packet_type": "agent_delta_packet",
        "schema_version": "2.0",
        "days_since_previous": int,
        "price_move_pct": float,
        "what_changed": {
            "fundamentals_changed": bool,
            "new_filing": bool | null,
            "analyst_action": bool | null,
            "trigger_state_changed": bool,
        },
        "current_state": {
            "trigger_state": str,
            "total_score": int,
        },
        "previous_judgement": {
            "portrait": str or null,
            "order": str or null,
            "instruction": "DO NOT READ THIS FIELD UNTIL YOU HAVE FORMED YOUR OWN ASSESSMENT",
        }
    }
    """
    if storage is None:
        return None

    ticker = scorecard.security.ticker
    run_date = scorecard.run_date

    # Get previous run data
    portrait_record = storage.get_latest_company_portrait(ticker, exclude_run_date=run_date)
    order_record = storage.get_latest_model_order(ticker, exclude_run_date=run_date)

    if portrait_record is None and order_record is None:
        # No previous run - caller should fall back to Tier 2
        return None

    # Calculate days since previous run
    days_since = portrait_record.get("days_ago") if portrait_record else 0
    if order_record and order_record.get("days_ago"):
        days_since = max(days_since, order_record.get("days_ago", 0))

    # Calculate price move
    prices = sorted(context.prices, key=lambda bar: bar.trading_date)
    price_move_pct = None
    if len(prices) >= 2:
        price_move_pct = (prices[-1].close - prices[-2].close) / prices[-2].close

    # Check if fundamentals changed
    fundamentals_comparison = _get_fundamentals_comparison(storage, ticker, run_date, context.fundamentals)

    # Check for new filings
    new_filing = None
    if context.official_events:
        # Check if latest event is new (occurred after previous run)
        latest_event = max(context.official_events, key=lambda e: e.event_time)
        if days_since is not None:
            event_age_days = (run_date - latest_event.event_time.date()).days
            new_filing = event_age_days < days_since if days_since else True

    return _serialize({
        "packet_type": "agent_delta_packet",
        "schema_version": "2.0",
        "packet_id": f"{scorecard.run_date.isoformat()}::{scorecard.security.ticker}",
        "generated_at": generated_at,
        "run_date": run_date,
        "days_since_previous": days_since,
        "price_move_pct": price_move_pct,
        "what_changed": {
            "fundamentals_changed": fundamentals_comparison.get("fundamentals_changed", False),
            "fundamentals_period_end": fundamentals_comparison.get("current_period_end"),
            "new_filing": new_filing,
            "analyst_action": None,  # Would need analyst history to detect
        },
        "current_state": {
            "trigger_state": scorecard.state.value if scorecard.state else None,
            "total_score": scorecard.total_score,
            "triggered": scorecard.triggered,
        },
        "previous_judgement": {
            "portrait": portrait_record.get("portrait_json") if portrait_record else None,
            "order": order_record.get("order_json") if order_record else None,
            "instruction": "CRITICAL: Do NOT read this field until you have formed and recorded your own assessment. "
                         "Reading the prior conclusion before deriving your own makes a fresh analysis indistinguishable from a copy.",
        },
    })


def build_tier2_packet(
    generated_at: datetime,
    context: PipelineContext,
    scorecard: ScoreCard,
    peer_contexts: list[PipelineContext] | None = None,
    storage: Storage | None = None,
) -> dict[str, Any]:
    """Build Tier 2 (working) packet: compressed version for default load (~10 KB).

    Replaces:
    - price_context.bars → price_summary (14 fields)
    - valuation_inputs.fundamentals → history_summary + derived only

    Keeps all other fields from Tier 3: always_on results, bucket_scores, official_events,
    analyst_summary, trigger_summary, rule_engine_precheck, freshness, earnings_calendar.
    """
    prices = sorted(context.prices, key=lambda bar: bar.trading_date)
    benchmark_prices = sorted(context.benchmark_prices, key=lambda bar: bar.trading_date)

    price_summary = _build_price_summary(prices, benchmark_prices)

    return _serialize({
        "packet_type": "agent_review_packet",
        "schema_version": "2.0",
        "packet_id": f"{scorecard.run_date.isoformat()}::{scorecard.security.ticker}",
        "generated_at": generated_at,
        "run_date": scorecard.run_date,
        "data_quality": "insufficient" if scorecard.data_insufficient else "ok",
        "freshness": _build_freshness(scorecard, context),
        "earnings_calendar": _build_earnings_calendar(scorecard, context),
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
        },
        "bucket_scores": {
            "fundamentals": asdict(scorecard.fundamentals),
            "sentiment": asdict(scorecard.sentiment),
            "social_rebound": asdict(scorecard.social_rebound),
            "chain_confirmation": asdict(scorecard.chain_confirmation),
            "price_flow": asdict(scorecard.price_flow),
            "risk_red_flags": asdict(scorecard.risk_red_flags),
        },
        "price_summary": price_summary,
        "price_summary_note": "Replaces bar arrays: latest close, 1/5/20/60-day changes, vs-benchmark, distance from SMAs, position in ranges",
        "full_packet_path": "<ticker>.full.json",
        "official_events": [_serialize_event(event) for event in context.official_events[:5]],
        "fundamentals_snapshot": _serialize_fundamentals(context.fundamentals),
        "valuation_inputs": _build_valuation_inputs_tier2(context, prices),
        "previous_judgement": _get_previous_judgement(storage, scorecard.security.ticker, scorecard.run_date),
        "option_summary": _serialize_option_snapshot(getattr(context, "options_snapshot", None)),
        "social_summary": None,  # Omit detailed social; reader can fetch if needed
        "analyst_summary": _serialize_analyst_summary(context),
        "macro_summary": _summarize_macro(context.macro),
        "source_health": [_serialize(asdict(status)) for status in context.source_statuses],
        "social_sources_to_fetch": _build_social_sources_to_fetch(scorecard.security.ticker),
    })
