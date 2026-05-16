"""
Decision condition evaluation engine.

Evaluates machine-readable invalidate/rerate conditions against fresh market data.
Pure deterministic Python with no LLM, storage, or pipeline imports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from market_sentiment.decision_schema import VALID_COMPARATORS, validate_decision_payload
from market_sentiment.models import EarningsCalendar, PriceBar


HORIZON_TRADING_DAYS = 30  # a recommendation auto-expires after this many trading days


@dataclass(frozen=True)
class DecisionAlert:
    """Result of evaluating a decision against fresh market data."""

    ticker: str
    decision_date: str
    kind: str  # "invalidated" | "rerated" | "expired"
    reason: str  # human-readable explanation
    fired_condition: dict | None  # the raw condition dict that fired, or None for expiry


def load_decision_files(decisions_dir: Path) -> tuple[list[dict], list[str]]:
    """
    Scan decisions_dir for *.decision.json files and load valid ones.

    Returns (valid_payloads, warnings).
    - Validates each file via validate_decision_payload.
    - Skips files with errors; adds warning strings for each error.
    - Returns ([], []) if dir doesn't exist.
    - Never raises; malformed JSON becomes a warning.
    """
    if not decisions_dir.exists():
        return ([], [])

    valid_payloads = []
    warnings = []

    for decision_file in sorted(decisions_dir.glob("*.decision.json")):
        try:
            with open(decision_file, encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            warnings.append(f"{decision_file.name}: {e}")
            continue

        # Validate the payload.
        errors = validate_decision_payload(payload)
        if errors:
            for error in errors:
                warnings.append(f"{decision_file.name}: {error}")
            continue

        valid_payloads.append(payload)

    return (valid_payloads, warnings)


def compute_metric(
    metric: str,
    *,
    bars: list[PriceBar],
    reference_close: float,
    decision_date: date,
    run_date: date,
    earnings: EarningsCalendar | None,
    as_of_index: int,
) -> float | None:
    """
    Compute a metric value for the bar at as_of_index.

    bars: sorted ascending by trading_date.
    as_of_index: which bar is "today" (-1 = latest, -2 = prior, etc).

    Returns None if the metric cannot be computed (insufficient data).
    """
    if not bars or abs(as_of_index) > len(bars):
        return None

    # Ensure valid indexing
    try:
        current_bar = bars[as_of_index]
    except IndexError:
        return None

    if metric == "close":
        return current_bar.close

    elif metric == "pct_from_reference":
        if reference_close <= 0:
            return None
        return (current_bar.close - reference_close) / reference_close

    elif metric == "close_vs_sma20":
        # Need 20 bars up to and including as_of_index.
        if as_of_index == -1:
            needed_index = len(bars) - 1
        else:
            needed_index = as_of_index
        start_index = needed_index - 19

        if start_index < 0 or needed_index >= len(bars):
            return None

        sma20 = sum(bars[i].close for i in range(start_index, needed_index + 1)) / 20
        if sma20 <= 0:
            return None
        return current_bar.close / sma20 - 1

    elif metric == "new_low_20d":
        # Check if current close is <= min close of 20 bars ending at as_of_index.
        if as_of_index == -1:
            needed_index = len(bars) - 1
        else:
            needed_index = as_of_index
        start_index = needed_index - 19

        if start_index < 0 or needed_index >= len(bars):
            return None

        min_close = min(bars[i].close for i in range(start_index, needed_index + 1))
        return 1.0 if current_bar.close <= min_close else 0.0

    elif metric == "days_held":
        # Count bars with trading_date > decision_date and <= current_bar.trading_date
        count = sum(
            1
            for bar in bars
            if decision_date < bar.trading_date <= current_bar.trading_date
        )
        return float(count)

    elif metric == "days_to_earnings":
        if earnings is None or earnings.next_earnings_date is None:
            return None
        return float((earnings.next_earnings_date - run_date).days)

    return None


def _apply_comparator(lhs: float, comparator: str, rhs: float) -> bool:
    """Apply a comparator between two floats."""
    if comparator == "<":
        return lhs < rhs
    elif comparator == "<=":
        return lhs <= rhs
    elif comparator == ">":
        return lhs > rhs
    elif comparator == ">=":
        return lhs >= rhs
    elif comparator == "==":
        return lhs == rhs
    return False


def evaluate_condition(
    condition: dict,
    *,
    bars: list[PriceBar],
    reference_close: float,
    decision_date: date,
    run_date: date,
    earnings: EarningsCalendar | None,
) -> bool:
    """
    Evaluate if a condition fires.

    For price-derived metrics (close, pct_from_reference, close_vs_sma20, new_low_20d):
    the condition fires only if the metric comparator threshold is true on EACH of
    the most recent window bars.

    For non-price metrics (days_held, days_to_earnings): evaluate once with as_of_index=-1,
    ignore window.

    Returns False if a required metric cannot be computed.
    """
    metric = condition.get("metric")
    comparator = condition.get("comparator")
    threshold = condition.get("threshold")
    window = max(1, int(condition.get("window", 1)))

    if metric is None or comparator is None or threshold is None:
        return False

    price_metrics = {"close", "pct_from_reference", "close_vs_sma20", "new_low_20d"}

    try:
        threshold = float(threshold)
    except (ValueError, TypeError):
        return False

    if metric in price_metrics:
        # Must hold on each of the last window bars.
        if len(bars) < window:
            return False

        for i in range(-window, 0):
            value = compute_metric(
                metric,
                bars=bars,
                reference_close=reference_close,
                decision_date=decision_date,
                run_date=run_date,
                earnings=earnings,
                as_of_index=i,
            )
            if value is None:
                return False
            if not _apply_comparator(value, comparator, threshold):
                return False
        return True

    else:
        # Non-price metric: evaluate once with as_of_index=-1.
        value = compute_metric(
            metric,
            bars=bars,
            reference_close=reference_close,
            decision_date=decision_date,
            run_date=run_date,
            earnings=earnings,
            as_of_index=-1,
        )
        if value is None:
            return False
        return _apply_comparator(value, comparator, threshold)


def _format_reason(condition: dict, metric_value: float, metric_name: str) -> str:
    """Format a human-readable reason string for a fired condition."""
    comparator = condition.get("comparator", "?")
    threshold = condition.get("threshold", "?")
    note = condition.get("note", "")

    reason = f"{metric_name} {comparator} {threshold} ({metric_value:.1f})"
    if note:
        reason += f" [{note}]"
    return reason


def evaluate_decision(
    decision: dict,
    *,
    bars: list[PriceBar],
    earnings: EarningsCalendar | None,
    run_date: date,
) -> DecisionAlert | None:
    """
    Evaluate a decision against fresh market data.

    Returns a DecisionAlert if the decision expired, was invalidated, or should be rerated.
    Returns None if the decision remains active.

    Precedence: expiry → invalidate → rerate.
    """
    try:
        decision_date = date.fromisoformat(decision.get("decision_date", ""))
        reference_close = float(decision.get("reference_close", 0))
        ticker = decision.get("ticker", "?")
        invalidate_conditions = decision.get("invalidate_conditions", [])
        rerate_conditions = decision.get("rerate_conditions", [])
    except (ValueError, TypeError):
        return None

    # Check expiry first.
    days_held_val = compute_metric(
        "days_held",
        bars=bars,
        reference_close=reference_close,
        decision_date=decision_date,
        run_date=run_date,
        earnings=earnings,
        as_of_index=-1,
    )
    if days_held_val is not None and days_held_val > HORIZON_TRADING_DAYS:
        return DecisionAlert(
            ticker=ticker,
            decision_date=decision.get("decision_date", ""),
            kind="expired",
            reason=f"horizon elapsed ({int(days_held_val)} trading days held)",
            fired_condition=None,
        )

    # Check invalidate conditions.
    for condition in invalidate_conditions:
        if evaluate_condition(
            condition,
            bars=bars,
            reference_close=reference_close,
            decision_date=decision_date,
            run_date=run_date,
            earnings=earnings,
        ):
            metric_name = condition.get("metric", "?")
            metric_value = compute_metric(
                metric_name,
                bars=bars,
                reference_close=reference_close,
                decision_date=decision_date,
                run_date=run_date,
                earnings=earnings,
                as_of_index=-1,
            )
            if metric_value is None:
                metric_value = 0.0

            reason = _format_reason(condition, metric_value, metric_name)
            return DecisionAlert(
                ticker=ticker,
                decision_date=decision.get("decision_date", ""),
                kind="invalidated",
                reason=reason,
                fired_condition=condition,
            )

    # Check rerate conditions.
    for condition in rerate_conditions:
        if evaluate_condition(
            condition,
            bars=bars,
            reference_close=reference_close,
            decision_date=decision_date,
            run_date=run_date,
            earnings=earnings,
        ):
            metric_name = condition.get("metric", "?")
            metric_value = compute_metric(
                metric_name,
                bars=bars,
                reference_close=reference_close,
                decision_date=decision_date,
                run_date=run_date,
                earnings=earnings,
                as_of_index=-1,
            )
            if metric_value is None:
                metric_value = 0.0

            reason = _format_reason(condition, metric_value, metric_name)
            return DecisionAlert(
                ticker=ticker,
                decision_date=decision.get("decision_date", ""),
                kind="rerated",
                reason=reason,
                fired_condition=condition,
            )

    return None
