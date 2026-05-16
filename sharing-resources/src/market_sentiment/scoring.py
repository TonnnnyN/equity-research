from __future__ import annotations

from datetime import date
from statistics import mean

from market_sentiment.models import (
    ActionState,
    BucketScore,
    EventTag,
    FundamentalSnapshot,
    OfficialEvent,
    OptionSnapshot,
    PipelineContext,
    PriceBar,
    ScoreCard,
    SocialSnapshot,
    SourceStatus,
)


NEGATIVE_KEYWORDS = {
    "bankruptcy",
    "fraud",
    "restatement",
    "delisting",
    "default",
    "investigation",
    "guidance cut",
}

RISKY_FORMS = {"NT 10-K", "NT 10-Q", "25-NSE", "RW"}


def classify_event_tag(current: PipelineContext, peer_contexts: list[PipelineContext]) -> EventTag:
    peer_layers = [
        peer for peer in peer_contexts if peer.security.layer == current.security.layer and peer.security.ticker != current.security.ticker
    ]
    same_layer_triggered = sum(
        1 for peer in peer_layers if len(peer.prices) >= 21 and peer.prices[-1].close <= min(bar.close for bar in peer.prices[-3:])
    )
    if len(peer_layers) and same_layer_triggered >= max(1, len(peer_layers) // 2):
        return EventTag.SECTOR_WIDE

    if len(current.benchmark_prices) >= 21:
        benchmark_return = 1 - (current.benchmark_prices[-1].close / current.benchmark_prices[-21].close)
        if benchmark_return >= 0.08:
            return EventTag.MARKET_WIDE

    return EventTag.COMPANY_SPECIFIC


def build_scorecard(
    run_date: date,
    context: PipelineContext,
    trigger,
    event_tag: EventTag,
    peer_contexts: list[PipelineContext],
) -> ScoreCard:
    fundamentals = score_fundamentals(run_date, context.official_events, context.fundamentals)
    sentiment = score_sentiment(run_date, context.official_events)
    partial_coverage = any(status.partial or not status.success for status in context.source_statuses)
    social_rebound = score_social_rebound(
        getattr(context, "social_snapshot", None),
        getattr(context, "social_source_statuses", []),
        partial_coverage=partial_coverage,
    )
    chain = score_chain(context, event_tag, peer_contexts)
    price_flow = score_price_flow(context.prices)
    risk, veto_reason = score_risk(context.official_events, trigger.reasons, context.fundamentals)

    base_total = fundamentals.score + sentiment.score + chain.score + price_flow.score + risk.score
    total = base_total + social_rebound.score
    base_state = map_state(base_total, veto_reason=veto_reason, trigger_reasons=trigger.reasons, new_low=trigger.new_low)
    candidate_state = map_state(total, veto_reason=veto_reason, trigger_reasons=trigger.reasons, new_low=trigger.new_low)
    state = apply_social_guardrail(base_state, candidate_state, social_rebound.score)
    state, data_insufficient = cap_state_if_data_insufficient(state, context.source_statuses)

    evidence = [event.form_type for event in context.official_events[:3]]
    if trigger.reasons:
        evidence.extend(trigger.reasons)
    social_state = getattr(getattr(context, "social_snapshot", None), "state", None)
    if social_state is not None:
        evidence.append(f"social:{social_state.value}")

    return ScoreCard(
        run_date=run_date,
        security=context.security,
        event_tag=event_tag,
        triggered=trigger.triggered,
        trigger=trigger,
        fundamentals=fundamentals,
        sentiment=sentiment,
        social_rebound=social_rebound,
        chain_confirmation=chain,
        price_flow=price_flow,
        risk_red_flags=risk,
        total_score=total,
        state=state if not veto_reason else ActionState.REJECT,
        veto_reason=veto_reason,
        partial_coverage=partial_coverage,
        data_insufficient=data_insufficient,
        evidence=evidence,
    )


def score_fundamentals(run_date: date, events: list[OfficialEvent], snapshot: FundamentalSnapshot | None) -> BucketScore:
    score = 6
    notes = []

    if snapshot:
        notes.append("companyfacts_snapshot_present")
        if snapshot.revenue_latest is not None:
            score += 6
        if snapshot.revenue_latest is not None and snapshot.revenue_previous not in (None, 0):
            revenue_growth = (snapshot.revenue_latest - snapshot.revenue_previous) / abs(snapshot.revenue_previous)
            score += 4 if revenue_growth >= 0 else 1
            notes.append("positive_revenue_growth" if revenue_growth >= 0 else "negative_revenue_growth")
        if snapshot.operating_cashflow_latest is not None:
            score += 5 if snapshot.operating_cashflow_latest > 0 else 1
            notes.append("positive_operating_cashflow" if snapshot.operating_cashflow_latest > 0 else "negative_operating_cashflow")
        if snapshot.cash_latest is not None and snapshot.debt_latest is not None:
            if snapshot.cash_latest >= snapshot.debt_latest:
                score += 4
                notes.append("cash_exceeds_debt")
            elif snapshot.cash_latest >= 0.5 * snapshot.debt_latest:
                score += 2
                notes.append("cash_covers_half_debt")
        if snapshot.capex_latest is not None:
            score += 2
            notes.append("capex_disclosed")
        if snapshot.filed_on:
            staleness = (run_date - snapshot.filed_on).days
            if staleness <= 45:
                score += 3
                notes.append("fresh_companyfacts_period")
            elif staleness > 100:
                score -= 2
                notes.append("stale_companyfacts_period")

    if events:
        recent_event = min((run_date - event.event_time.date()).days for event in events)
        if recent_event <= 15:
            score += 5
            notes.append("fresh_official_updates")
        elif recent_event <= 45:
            score += 3
            notes.append("moderately_fresh_updates")
        else:
            score += 1
            notes.append("stale_updates")

        filing_forms = {event.form_type for event in events}
        if {"10-Q", "10-K"} & filing_forms:
            score += 3
            notes.append("core_financial_filing_present")
        if "8-K" in filing_forms:
            score += 2
            notes.append("recent_current_report")
    elif snapshot is None:
        return BucketScore("fundamentals", 4, 30, ["no_official_events_or_companyfacts"])

    return BucketScore("fundamentals", min(score, 30), 30, notes)


def score_sentiment(run_date: date, events: list[OfficialEvent]) -> BucketScore:
    if not events:
        return BucketScore("sentiment", 3, 15, ["missing_event_text"])
    score = 6
    notes = []
    titles = " ".join(event.title.lower() for event in events[:10])
    freshest_days = min((run_date - event.event_time.date()).days for event in events)
    if freshest_days <= 15:
        score += 3
        notes.append("fresh_disclosure_window")
    elif freshest_days <= 45:
        score += 2
        notes.append("recent_disclosure_window")
    if any(event.form_type in {"10-Q", "10-K", "8-K"} for event in events[:5]):
        score += 3
        notes.append("current_reporting_active")
    if any(event.form_type in RISKY_FORMS for event in events[:5]):
        score -= 4
        notes.append("late_or_withdrawn_form_detected")
    if any(keyword in titles for keyword in {"results", "earnings", "quarterly", "annual"}):
        score += 2
        notes.append("financial_update_detected")
    if any(keyword in titles for keyword in NEGATIVE_KEYWORDS):
        score -= 4
        notes.append("negative_keyword_detected")
    return BucketScore("sentiment", max(0, min(score, 15)), 15, notes)


def score_social_rebound(
    snapshot: SocialSnapshot | None,
    statuses: list,
    *,
    partial_coverage: bool,
) -> BucketScore:
    if snapshot is None:
        return BucketScore("social_rebound", 0, 10, ["social_unavailable"])
    notes = list(snapshot.notes)
    if any(not status.success for status in statuses):
        notes.append("social_source_fetch_failed")
    state_note_map = {
        "strong_rebound": "social_strong_rebound",
        "mild_rebound": "social_mild_rebound",
        "flat_unclear": "social_flat_unclear",
        "worsening": "social_worsening",
        "insufficient_data": "social_insufficient_data",
    }
    notes.append(state_note_map.get(snapshot.state.value, snapshot.state.value))
    score = snapshot.score
    if partial_coverage and score > 0:
        score = 0
        notes.append("social_positive_blocked_by_partial_core_coverage")
    elif partial_coverage and score < 0:
        score = -(abs(score) // 2)  # halve the penalty, round toward zero
        notes.append("social_negative_halved_under_partial_coverage")
    return BucketScore("social_rebound", max(-6, min(score, 10)), 10, notes)


def score_chain(context: PipelineContext, event_tag: EventTag, peer_contexts: list[PipelineContext]) -> BucketScore:
    score = 12
    notes = []
    same_layer_count = sum(1 for peer in peer_contexts if peer.security.layer == context.security.layer)
    if same_layer_count:
        score += 2
        notes.append("peer_coverage_present")
    if event_tag == EventTag.COMPANY_SPECIFIC:
        score += 6
        notes.append("isolated_dip")
    elif event_tag == EventTag.SECTOR_WIDE:
        score += 2
        notes.append("sector_pressure")
    else:
        score -= 4
        notes.append("market_pressure")
    if context.macro:
        score += 2
        notes.append("macro_context_present")
    option_score, option_notes = _score_options_confirmation(getattr(context, "options_snapshot", None))
    score += option_score
    notes.extend(option_notes)
    return BucketScore("chain_confirmation", max(0, min(score, 20)), 20, notes)


def score_price_flow(security_prices: list[PriceBar]) -> BucketScore:
    """Score post-drop price action: is the stock stabilizing or still deteriorating?

    Measures three components of post-drop price behavior:
    - Stabilization: no new lows + bounce off 20-day low (0-6 pts)
    - Short-MA reclaim: above SMA5/SMA10 (0-5 pts)
    - Close-location strength: closes near daily highs (0-4 pts)

    Total range: 0-15 points.
    """
    notes = []

    # Defensive sort
    bars = sorted(security_prices, key=lambda b: b.trading_date)

    # Insufficient-data guard
    if len(bars) < 21:
        return BucketScore("price_flow", 0, 15, ["insufficient_price_history"])

    latest = bars[-1]

    # Component ① — Stabilization (0–6 points)
    recent_min = min(b.close for b in bars[-3:])
    prior_min = min(b.close for b in bars[-20:-3])

    points_1a = 0
    if recent_min >= prior_min:
        points_1a = 3
        notes.append("basing_no_new_lows")
    else:
        notes.append("still_making_lows")

    # Bounce off 20-day low
    window_min = min(b.close for b in bars[-20:])
    if window_min <= 0:
        bounce_pct = 0.0
    else:
        bounce_pct = (latest.close - window_min) / window_min

    points_1b = min(3, max(0, round(bounce_pct * 60)))
    notes.append(f"bounce_off_low_{bounce_pct*100:.1f}pct")

    component1 = points_1a + points_1b

    # Component ② — Short-MA reclaim (0–5 points)
    sma5 = mean(b.close for b in bars[-5:])
    sma10 = mean(b.close for b in bars[-10:])

    points_2 = 0
    if latest.close > sma5:
        points_2 += 2
        notes.append("above_sma5")
    else:
        notes.append("below_sma5")

    if latest.close > sma10:
        points_2 += 2
        notes.append("above_sma10")
    else:
        notes.append("below_sma10")

    if sma5 > sma10:
        points_2 += 1
        notes.append("sma5_above_sma10")

    component2 = points_2

    # Component ③ — Close-location strength (0–4 points)
    close_locations = []
    for b in bars[-5:]:
        rng = b.high - b.low
        cl = 0.5 if rng <= 0 else (b.close - b.low) / rng
        close_locations.append(cl)

    avg_cl = mean(close_locations)
    points_3 = min(4, max(0, round((avg_cl - 0.3) * 10)))
    notes.append(f"close_location_{avg_cl:.2f}")

    component3 = points_3

    # Final score
    score = component1 + component2 + component3
    return BucketScore("price_flow", max(0, min(score, 15)), 15, notes)


def score_risk(
    events: list[OfficialEvent],
    trigger_reasons: list[str],
    snapshot: FundamentalSnapshot | None,
) -> tuple[BucketScore, str | None]:
    score = 20
    notes = []
    veto_reason = None
    titles = " ".join(event.title.lower() for event in events[:10])
    if any(keyword in titles for keyword in NEGATIVE_KEYWORDS):
        veto_reason = "negative_official_keyword"
        score = 0
        notes.append("hard_veto_negative_keyword")
    if any(event.form_type in RISKY_FORMS for event in events[:5]):
        score -= 4
        notes.append("elevated_form_risk")
    if snapshot:
        if snapshot.revenue_latest is not None and snapshot.revenue_previous not in (None, 0):
            revenue_growth = (snapshot.revenue_latest - snapshot.revenue_previous) / abs(snapshot.revenue_previous)
            if revenue_growth < -0.25:
                score -= 5
                notes.append("companyfacts_revenue_break")
        if snapshot.operating_cashflow_latest is not None and snapshot.operating_cashflow_latest < 0:
            score -= 4
            notes.append("companyfacts_negative_operating_cashflow")
        if (
            snapshot.revenue_latest is not None
            and snapshot.revenue_previous not in (None, 0)
            and snapshot.operating_cashflow_latest is not None
        ):
            revenue_growth = (snapshot.revenue_latest - snapshot.revenue_previous) / abs(snapshot.revenue_previous)
            if revenue_growth < -0.35 and snapshot.operating_cashflow_latest < 0 and "fresh_low" in trigger_reasons:
                veto_reason = "companyfacts_structural_break"
                score = 0
                notes.append("hard_veto_structural_break")
    if "fresh_low" in trigger_reasons:
        score -= 3
        notes.append("still_making_lows")
    if not events:
        score -= 3
        notes.append("no_official_coverage")
    score = max(score, 0)
    return BucketScore("risk_red_flags", score, 20, notes), veto_reason


def map_state(total_score: int, veto_reason: str | None, trigger_reasons: list[str], new_low: bool) -> ActionState:
    if veto_reason:
        return ActionState.REJECT
    if total_score < 65:
        return ActionState.REJECT
    if total_score < 72:
        return ActionState.WATCH
    if new_low and "fresh_low" in trigger_reasons:
        return ActionState.WATCH
    if total_score < 80:
        return ActionState.STARTER
    return ActionState.ADD


def apply_social_guardrail(base_state: ActionState, candidate_state: ActionState, social_score: int) -> ActionState:
    if social_score <= 0:
        return candidate_state
    if candidate_state == ActionState.ADD and base_state != ActionState.ADD:
        return ActionState.STARTER if base_state != ActionState.REJECT else ActionState.WATCH
    if base_state == ActionState.REJECT and candidate_state not in {ActionState.REJECT, ActionState.WATCH}:
        return ActionState.WATCH
    return candidate_state


def cap_state_if_data_insufficient(state: ActionState, source_health: list[SourceStatus]) -> tuple[ActionState, bool]:
    """Cap state at WATCH when core data lanes (SEC + a fresh non-partial price source) are missing.

    A price source counts as "fresh" only when success=True and partial=False.
    daily_prices_cache is intentionally excluded because pipeline marks it
    partial=True (cache fallback should not elevate confidence).

    Returns (possibly-downgraded state, insufficient_flag).
    """
    sec_ok = any(
        s.source == "sec" and s.success and not s.partial
        for s in source_health
    )
    price_ok = any(
        s.source in ("tiger", "yahoo_chart", "alpha_vantage", "stooq") and s.success and not s.partial
        for s in source_health
    )
    if sec_ok and price_ok:
        return state, False
    # core data insufficient — cap upside only if both are bad
    insufficient = not (sec_ok and price_ok)
    if insufficient and state in (ActionState.ADD, ActionState.STARTER):
        return ActionState.WATCH, True
    return state, insufficient


def _score_options_confirmation(snapshot: OptionSnapshot | None) -> tuple[int, list[str]]:
    if snapshot is None:
        return 0, ["options_unavailable"]

    notes = ["options_activity_present"]
    score = 0
    volume_ratio = snapshot.put_call_volume_ratio
    oi_ratio = snapshot.put_call_open_interest_ratio
    if volume_ratio is not None and volume_ratio < 0.8:
        score += 2
        notes.append("options_call_skew_constructive")
    elif volume_ratio is not None and volume_ratio >= 1.2:
        score -= 2
        notes.append("options_put_skew_defensive")
    if oi_ratio is not None and oi_ratio < 0.85:
        score += 1
        notes.append("options_open_interest_constructive")
    elif oi_ratio is not None and oi_ratio >= 1.15:
        score -= 1
        notes.append("options_open_interest_defensive")
    if snapshot.nearest_days_to_expiry is not None and 0 <= snapshot.nearest_days_to_expiry <= 10:
        notes.append("options_near_expiry_window")
    return score, notes
