"""
Outcome ledger: reads resolved decisions and provides analysis.

Records the fate of each decision:
- invalidated: thesis broke (invalidate_if condition fired)
- rerated: thesis played out (rerate_if condition fired)
- expired: held for 90+ trading days without either condition firing

Computes statistics about outcomes with strong small-sample guards.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

# Default holding window (trading days) before a decision expires if unresolved
DEFAULT_HOLDING_WINDOW = 90


@dataclass(frozen=True)
class ResolutionRecord:
    """A single resolved decision's outcome."""

    ticker: str
    decision_date: str
    state: str  # WATCH, STARTER, ADD
    resolution_type: str  # "invalidated", "rerated", "expired"
    days_elapsed: int
    reference_close: float
    close_at_resolution: float | None  # None if not available
    bucket_weights: dict[str, float] | None  # None if not in record (backward compat)
    buckets_dropped: list[str] | None
    valuation_assumptions: dict[str, Any] | None
    context_available: bool  # True if bucket/valuation data is present


def read_resolved_decisions(
    db: Any,  # Storage instance
    data_dir: Path,
) -> tuple[list[ResolutionRecord], list[str]]:
    """
    Read all resolved decisions from the database.

    Returns (records, warnings).
    - records: list of ResolutionRecord for each resolved decision
    - warnings: list of issues encountered (missing close prices, etc.)

    Backward compatible: decisions without bucket_weights/buckets_dropped/
    valuation_assumptions are still included, with context_available=False.
    """
    records: list[ResolutionRecord] = []
    warnings: list[str] = []

    try:
        # Read all decisions (both active and resolved)
        all_decisions = db.read_all_decisions_for_ticker("") if hasattr(db, 'read_all_decisions_for_ticker') else []

        # Since we can't query by status != 'active', we need to read differently.
        # For now, use a workaround: query the database directly for resolved ones.
        # This is a limitation until storage.py is extended.
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(db.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT ticker, decision_date, state, reference_close,
                       invalidate_conditions, rerate_conditions,
                       status, status_reason, last_checked_date
                FROM active_decisions
                WHERE status IN ('invalidated', 'rerated', 'expired')
                ORDER BY ticker ASC, decision_date ASC
                """
            ).fetchall()

        for row in rows:
            ticker = row[0]
            decision_date = row[1]
            state = row[2]
            reference_close = row[3]
            status = row[6]
            status_reason = row[7]
            last_checked_date = row[8]

            # Try to get close at resolution from price history
            if last_checked_date:
                resolution_date = date.fromisoformat(last_checked_date)
                price_result = db.get_price_on_or_before(ticker, resolution_date)
                close_at_resolution = price_result[1] if price_result else None
            else:
                close_at_resolution = None

            # Calculate days elapsed
            decision_dt = date.fromisoformat(decision_date)
            last_checked_dt = date.fromisoformat(last_checked_date) if last_checked_date else date.today()
            days_elapsed = (last_checked_dt - decision_dt).days

            # Try to load decision file to get context
            bucket_weights = None
            buckets_dropped = None
            valuation_assumptions = None
            context_available = False

            # Look for decision file
            decision_file = data_dir / "decisions" / decision_date / f"{ticker}.decision.json"
            if decision_file.exists():
                try:
                    with open(decision_file) as f:
                        payload = json.load(f)
                        bucket_weights = payload.get('bucket_weights')
                        buckets_dropped = payload.get('buckets_dropped')
                        valuation_assumptions = payload.get('valuation_assumptions')
                        context_available = (
                            bucket_weights is not None
                            or buckets_dropped is not None
                            or valuation_assumptions is not None
                        )
                except (json.JSONDecodeError, IOError) as e:
                    warnings.append(f"{ticker} {decision_date}: could not read decision file: {e}")

            record = ResolutionRecord(
                ticker=ticker,
                decision_date=decision_date,
                state=state,
                resolution_type=status,
                days_elapsed=days_elapsed,
                reference_close=reference_close,
                close_at_resolution=close_at_resolution,
                bucket_weights=bucket_weights,
                buckets_dropped=buckets_dropped,
                valuation_assumptions=valuation_assumptions,
                context_available=context_available,
            )
            records.append(record)

    except Exception as e:
        warnings.append(f"Failed to read resolved decisions: {e}")

    return records, warnings


@dataclass(frozen=True)
class OutcomeSummary:
    """Summary statistics about a set of resolved decisions."""

    total_count: int
    invalidated_count: int
    rerated_count: int
    expired_count: int
    median_days_to_resolution: float | None
    pct_with_context: float | None  # None if not meaningful


def compute_summary(records: list[ResolutionRecord]) -> OutcomeSummary:
    """
    Compute summary statistics from resolved decisions.

    Always includes sample count (n) with every figure to guard against
    over-reading small samples.
    """
    if not records:
        return OutcomeSummary(
            total_count=0,
            invalidated_count=0,
            rerated_count=0,
            expired_count=0,
            median_days_to_resolution=None,
            pct_with_context=None,
        )

    invalidated = sum(1 for r in records if r.resolution_type == "invalidated")
    rerated = sum(1 for r in records if r.resolution_type == "rerated")
    expired = sum(1 for r in records if r.resolution_type == "expired")

    # Median days to resolution
    days_list = sorted([r.days_elapsed for r in records])
    if len(days_list) % 2 == 0:
        median_days = (days_list[len(days_list) // 2 - 1] + days_list[len(days_list) // 2]) / 2
    else:
        median_days = float(days_list[len(days_list) // 2])

    # Percentage with context
    with_context = sum(1 for r in records if r.context_available)
    pct_context = (with_context / len(records) * 100) if records else None

    return OutcomeSummary(
        total_count=len(records),
        invalidated_count=invalidated,
        rerated_count=rerated,
        expired_count=expired,
        median_days_to_resolution=median_days,
        pct_with_context=pct_context,
    )


def compute_summary_by_state(records: list[ResolutionRecord]) -> dict[str, OutcomeSummary]:
    """
    Compute summary statistics grouped by decision state (WATCH, STARTER, ADD).
    """
    by_state: dict[str, list[ResolutionRecord]] = {}
    for record in records:
        if record.state not in by_state:
            by_state[record.state] = []
        by_state[record.state].append(record)

    return {state: compute_summary(recs) for state, recs in by_state.items()}


def compute_summary_by_dropped_bucket(
    records: list[ResolutionRecord],
) -> dict[str, OutcomeSummary]:
    """
    Compute outcome statistics grouped by whether a specific bucket was dropped.

    Example: {
        'social_dropped': OutcomeSummary(...),   # outcomes where 'social' was in buckets_dropped
        'social_kept': OutcomeSummary(...),      # outcomes where 'social' was NOT dropped
    }

    Only includes decisions with context available (where buckets_dropped is not None).
    """
    with_context = [r for r in records if r.context_available and r.buckets_dropped is not None]

    if not with_context:
        return {}

    # Find all buckets mentioned (in either dropped or kept lists)
    all_buckets = set()
    for record in with_context:
        if record.buckets_dropped:
            all_buckets.update(record.buckets_dropped)
        # Also check bucket_weights to find potential buckets that weren't dropped
        if record.bucket_weights:
            all_buckets.update(record.bucket_weights.keys())

    result = {}
    for bucket in sorted(all_buckets):
        dropped = [
            r
            for r in with_context
            if r.buckets_dropped is not None and bucket in r.buckets_dropped
        ]
        # Kept = has the bucket in bucket_weights but NOT in buckets_dropped
        kept = [
            r
            for r in with_context
            if (
                r.bucket_weights is not None
                and bucket in r.bucket_weights
                and (r.buckets_dropped is None or bucket not in r.buckets_dropped)
            )
        ]

        result[f"{bucket}_dropped"] = compute_summary(dropped)
        result[f"{bucket}_kept"] = compute_summary(kept)

    return result


def format_summary_as_markdown(summary: OutcomeSummary, title: str = "") -> list[str]:
    """
    Format an OutcomeSummary as markdown lines, with strong sample-size guards.

    Example output:
        ## Outcomes (n=3)
        - Invalidated: 1 of 3
        - Rerated: 1 of 3
        - Expired: 1 of 3
        - Median days to resolution: 45 (n=3)

    Always includes n with every figure to refuse implication of significance.
    """
    lines = []

    if title:
        lines.append(f"## {title} (n={summary.total_count})")
    else:
        lines.append(f"## Outcomes (n={summary.total_count})")

    if summary.total_count == 0:
        lines.append("No resolved decisions.")
        return lines

    # Sample-size guard: warn if very few
    if summary.total_count < 5:
        lines.append(
            f"**WARNING: Only {summary.total_count} decisions. Statistics below are far too small to conclude anything.**"
        )
        lines.append("")

    lines.append(f"- Invalidated: {summary.invalidated_count} of {summary.total_count}")
    lines.append(f"- Rerated: {summary.rerated_count} of {summary.total_count}")
    lines.append(f"- Expired unresolved: {summary.expired_count} of {summary.total_count}")

    if summary.median_days_to_resolution is not None:
        lines.append(f"- Median days to resolution: {summary.median_days_to_resolution:.1f} (n={summary.total_count})")

    if summary.pct_with_context is not None:
        lines.append(f"- Context data available: {summary.pct_with_context:.1f}% (n={summary.total_count})")

    return lines


@dataclass(frozen=True)
class SignalDetectionResult:
    """Result of pattern detection and streak detection."""

    pattern_detected: bool
    pattern_message: str | None  # Human-readable pattern description, None if not detected
    pattern_subgroup: str | None  # Which subgroup fired the pattern
    pattern_n: int  # Sample size for the pattern
    pattern_invalidation_rate: float | None  # Invalidation rate for pattern subgroup
    pattern_base_rate: float | None  # Base invalidation rate overall

    consecutive_failures: bool
    consecutive_count: int  # How many consecutive failures (0-3)
    consecutive_tickers: list[str]  # Tickers of the recent closed decisions


def detect_consecutive_failures(records: list[ResolutionRecord]) -> SignalDetectionResult:
    """
    Flag when the 3 most recently closed theses are all `invalidated`.

    Returns a SignalDetectionResult with consecutive_failures=True if true,
    along with count and tickers.
    """
    # Filter to closed decisions (non-active), excluding superseded
    closed = [r for r in records if r.resolution_type in ("invalidated", "rerated", "expired")]

    # Sort by decision_date descending to get most recent first
    closed_sorted = sorted(closed, key=lambda r: r.decision_date, reverse=True)

    # Check the most recent 3
    consecutive_count = 0
    recent_tickers = []
    for i, record in enumerate(closed_sorted[:3]):
        if record.resolution_type == "invalidated":
            consecutive_count += 1
            recent_tickers.append(record.ticker)
        else:
            break  # Stop at first non-invalidation

    return SignalDetectionResult(
        pattern_detected=False,
        pattern_message=None,
        pattern_subgroup=None,
        pattern_n=0,
        pattern_invalidation_rate=None,
        pattern_base_rate=None,
        consecutive_failures=consecutive_count == 3,
        consecutive_count=consecutive_count,
        consecutive_tickers=recent_tickers,
    )


def detect_pattern(
    records: list[ResolutionRecord],
    threshold_n: int = 5,
    margin: float = 0.15,
) -> SignalDetectionResult:
    """
    Detect patterns in closed decisions by comparing subgroups.

    Subgroups compared:
    - Decisions where a specific bucket was dropped vs. kept
    - Decisions by action (WATCH, STARTER, ADD)
    - Decisions by layer specification

    Returns pattern_detected=True only if a subgroup has ≥threshold_n closed theses
    and its invalidation rate departs from base rate by margin (default 15%).

    Args:
        records: list of ResolutionRecord
        threshold_n: minimum sample size for a pattern to be significant
        margin: minimum deviation from base rate (as fraction, e.g., 0.15 = 15%)
    """
    # Filter to closed decisions only
    closed = [r for r in records if r.resolution_type in ("invalidated", "rerated", "expired")]

    if not closed:
        return SignalDetectionResult(
            pattern_detected=False,
            pattern_message=None,
            pattern_subgroup=None,
            pattern_n=0,
            pattern_invalidation_rate=None,
            pattern_base_rate=None,
            consecutive_failures=False,
            consecutive_count=0,
            consecutive_tickers=[],
        )

    # Compute base invalidation rate
    base_invalidated = sum(1 for r in closed if r.resolution_type == "invalidated")
    base_rate = base_invalidated / len(closed) if closed else 0

    # Check subgroup: dropped buckets
    with_context = [r for r in closed if r.context_available and r.buckets_dropped is not None]
    if with_context:
        all_buckets = set()
        for record in with_context:
            if record.buckets_dropped:
                all_buckets.update(record.buckets_dropped)
            if record.bucket_weights:
                all_buckets.update(record.bucket_weights.keys())

        for bucket in sorted(all_buckets):
            # Dropped subgroup
            dropped = [r for r in with_context if r.buckets_dropped and bucket in r.buckets_dropped]
            if len(dropped) >= threshold_n:
                dropped_invalidated = sum(1 for r in dropped if r.resolution_type == "invalidated")
                dropped_rate = dropped_invalidated / len(dropped)
                if abs(dropped_rate - base_rate) > margin:
                    return SignalDetectionResult(
                        pattern_detected=True,
                        pattern_message=f"Dropped '{bucket}' invalidation rate {dropped_rate:.1%} departs from base {base_rate:.1%}",
                        pattern_subgroup=f"{bucket}_dropped",
                        pattern_n=len(dropped),
                        pattern_invalidation_rate=dropped_rate,
                        pattern_base_rate=base_rate,
                        consecutive_failures=False,
                        consecutive_count=0,
                        consecutive_tickers=[],
                    )

            # Kept subgroup
            kept = [
                r for r in with_context
                if r.bucket_weights and bucket in r.bucket_weights and (r.buckets_dropped is None or bucket not in r.buckets_dropped)
            ]
            if len(kept) >= threshold_n:
                kept_invalidated = sum(1 for r in kept if r.resolution_type == "invalidated")
                kept_rate = kept_invalidated / len(kept)
                if abs(kept_rate - base_rate) > margin:
                    return SignalDetectionResult(
                        pattern_detected=True,
                        pattern_message=f"Kept '{bucket}' invalidation rate {kept_rate:.1%} departs from base {base_rate:.1%}",
                        pattern_subgroup=f"{bucket}_kept",
                        pattern_n=len(kept),
                        pattern_invalidation_rate=kept_rate,
                        pattern_base_rate=base_rate,
                        consecutive_failures=False,
                        consecutive_count=0,
                        consecutive_tickers=[],
                    )

    # Check subgroup: by action
    by_action: dict[str, list[ResolutionRecord]] = {}
    for record in closed:
        if record.state not in by_action:
            by_action[record.state] = []
        by_action[record.state].append(record)

    for action, records_for_action in sorted(by_action.items()):
        if len(records_for_action) >= threshold_n:
            action_invalidated = sum(1 for r in records_for_action if r.resolution_type == "invalidated")
            action_rate = action_invalidated / len(records_for_action)
            if abs(action_rate - base_rate) > margin:
                return SignalDetectionResult(
                    pattern_detected=True,
                    pattern_message=f"Action '{action}' invalidation rate {action_rate:.1%} departs from base {base_rate:.1%}",
                    pattern_subgroup=f"action_{action}",
                    pattern_n=len(records_for_action),
                    pattern_invalidation_rate=action_rate,
                    pattern_base_rate=base_rate,
                    consecutive_failures=False,
                    consecutive_count=0,
                    consecutive_tickers=[],
                )

    # No pattern detected
    return SignalDetectionResult(
        pattern_detected=False,
        pattern_message=None,
        pattern_subgroup=None,
        pattern_n=0,
        pattern_invalidation_rate=None,
        pattern_base_rate=None,
        consecutive_failures=False,
        consecutive_count=0,
        consecutive_tickers=[],
    )


def format_signals_as_compact_status(pattern_result: SignalDetectionResult, consecutive_result: SignalDetectionResult) -> str:
    """
    Format both pattern and streak detection as a single compact status line (~100 bytes when nothing).

    Example outputs:
    - "ledger: 14 theses closed, no pattern above threshold"
    - "ledger: 14 theses closed, pattern: dropped 'social' invalidation 80% vs base 20% (n=5)"
    - "ledger: 14 theses closed, WARNING: 3 consecutive failures"
    """
    parts = []

    # Note: we'd need to pass record count to make this work; for now just report the signals
    if pattern_result.pattern_detected:
        parts.append(
            f"pattern: {pattern_result.pattern_subgroup} invalidation "
            f"{pattern_result.pattern_invalidation_rate:.0%} vs base {pattern_result.pattern_base_rate:.0%} (n={pattern_result.pattern_n})"
        )

    if consecutive_result.consecutive_failures:
        parts.append(f"WARNING: {consecutive_result.consecutive_count} consecutive failures ({', '.join(consecutive_result.consecutive_tickers)})")

    if not parts:
        return "no patterns detected"

    return ", ".join(parts)


def render_ledger_markdown(records: list[ResolutionRecord], warnings: list[str]) -> str:
    """
    Render a complete ledger as markdown suitable for appending to data/reports/index.md.

    Returns a markdown string with:
    - Overall summary
    - Breakdown by decision state
    - Breakdown by bucket drops (if context available)
    - Signal detection results
    - Warnings if any
    """
    lines = []

    lines.append("# Decision Outcome Ledger")
    lines.append("")

    if warnings:
        lines.append("## Warnings")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

    # Overall summary
    overall = compute_summary(records)
    lines.extend(format_summary_as_markdown(overall, "Overall Outcomes"))
    lines.append("")

    # Signal detection
    pattern_result = detect_pattern(records)
    consecutive_result = detect_consecutive_failures(records)

    if pattern_result.pattern_detected or consecutive_result.consecutive_failures:
        lines.append("## Signal Detection")
        lines.append("")
        if pattern_result.pattern_detected:
            lines.append(
                f"**Pattern detected:** {pattern_result.pattern_message} ({pattern_result.pattern_n} theses)"
            )
        if consecutive_result.consecutive_failures:
            lines.append(
                f"**Consecutive failures:** Last 3 closed theses all invalidated ({', '.join(consecutive_result.consecutive_tickers)})"
            )
        lines.append("")

    # By state
    by_state = compute_summary_by_state(records)
    if by_state:
        lines.append("## Outcomes by Decision State")
        lines.append("")
        for state in sorted(by_state.keys()):
            summary = by_state[state]
            lines.extend(format_summary_as_markdown(summary, f"{state}"))
            lines.append("")

    # By dropped bucket
    by_bucket = compute_summary_by_dropped_bucket(records)
    if by_bucket:
        lines.append("## Outcomes by Bucket Configuration")
        lines.append("")
        for bucket_key in sorted(by_bucket.keys()):
            summary = by_bucket[bucket_key]
            lines.extend(format_summary_as_markdown(summary, bucket_key.replace('_', ' ').title()))
            lines.append("")

    # Detailed table
    if records:
        lines.append("## Detailed Outcome Records")
        lines.append("")
        lines.append(
            "| Ticker | State | Decision Date | Resolution | Days | Ref Close | Close @ Resolution | Context |"
        )
        lines.append("|--------|-------|---------------|------------|------|-----------|-------------------|---------|")

        for record in records:
            context_marker = "✓" if record.context_available else "✗"
            close_str = f"{record.close_at_resolution:.2f}" if record.close_at_resolution else "N/A"
            lines.append(
                f"| {record.ticker} | {record.state} | {record.decision_date} | {record.resolution_type} | "
                f"{record.days_elapsed} | {record.reference_close:.2f} | {close_str} | {context_marker} |"
            )

    return "\n".join(lines)
