"""Status engine: replays the rule engine over a historical dataset.

For every decision date in the backtest window and every US security it builds
a point-in-time PipelineContext (prices/events/fundamentals filtered as-of the
date, social and options omitted), runs the production trigger + scorecard
logic, and emits one status record per triggered stock.

The output feeds simulator.run_simulation.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from market_sentiment.backtest.asof_fundamentals import build_fundamentals_timeline, snapshot_asof
from market_sentiment.config import load_config
from market_sentiment.models import (
    OfficialEvent,
    PipelineContext,
    PriceBar,
    Security,
    SourceStatus,
)
from market_sentiment.scoring import build_scorecard, classify_event_tag
from market_sentiment.triggers import compute_trigger

# Synthetic source statuses: the backtest dataset is treated as full, fresh
# coverage so cap_state_if_data_insufficient / partial_coverage do not fire.
_FULL_COVERAGE_STATUSES = [
    SourceStatus(source="sec", success=True, partial=False, message="backtest dataset"),
    SourceStatus(source="yahoo_chart", success=True, partial=False, message="backtest dataset"),
]

_MIN_HISTORY_BARS = 21


def _parse_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def load_raw_price_bars(dataset_dir: str | Path) -> dict[str, list[dict]]:
    """Load raw price-bar dicts keyed by ticker (consumed by the simulator)."""
    prices_dir = Path(dataset_dir) / "prices"
    out: dict[str, list[dict]] = {}
    for path in sorted(prices_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        bars = sorted(payload.get("bars", []), key=lambda b: b["date"])
        out[payload["ticker"]] = bars
    return out


def _load_price_models(dataset_dir: Path) -> dict[str, list[PriceBar]]:
    prices_dir = dataset_dir / "prices"
    out: dict[str, list[PriceBar]] = {}
    for path in sorted(prices_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        ticker = payload["ticker"]
        bars = []
        for b in payload.get("bars", []):
            bars.append(
                PriceBar(
                    ticker=ticker,
                    trading_date=_parse_date(b["date"]),
                    open=float(b["open"] or 0.0),
                    high=float(b["high"] or 0.0),
                    low=float(b["low"] or 0.0),
                    close=float(b["close"] or 0.0),
                    volume=float(b["volume"]) if b.get("volume") is not None else None,
                    source="yahoo_chart",
                )
            )
        bars.sort(key=lambda bar: bar.trading_date)
        out[ticker] = bars
    return out


def _load_events(dataset_dir: Path) -> dict[str, list[OfficialEvent]]:
    events_dir = dataset_dir / "sec_events"
    out: dict[str, list[OfficialEvent]] = defaultdict(list)
    if not events_dir.exists():
        return out
    for path in sorted(events_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        ticker = payload["ticker"]
        for e in payload.get("events", []):
            out[ticker].append(
                OfficialEvent(
                    ticker=ticker,
                    event_time=datetime.strptime(e["event_time"][:19], "%Y-%m-%dT%H:%M:%S"),
                    form_type=e["form_type"],
                    title=e.get("title", ""),
                    url=e.get("url", ""),
                    source="sec",
                )
            )
        out[ticker].sort(key=lambda ev: ev.event_time)
    return out


def _load_fundamentals_timelines(dataset_dir: Path) -> dict[str, list]:
    facts_dir = dataset_dir / "companyfacts"
    out: dict[str, list] = {}
    if not facts_dir.exists():
        return out
    for path in sorted(facts_dir.glob("*.json")):
        ticker = path.stem
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        cik = payload.get("cik")
        cik_str = str(cik).zfill(10) if cik is not None else None
        out[ticker] = build_fundamentals_timeline(payload, ticker, cik_str)
    return out


def compute_statuses(
    dataset_dir: str | Path,
    config_path: str = "config/watchlist.toml",
    start_date: date | str | None = None,
    end_date: date | str | None = None,
) -> list[dict]:
    """Replay the rule engine over the dataset, returning triggered status records.

    Args:
        dataset_dir: directory produced by build_dataset.
        config_path: watchlist TOML (for layers, thresholds, benchmarks).
        start_date / end_date: inclusive decision-date window. Defaults to the
            full price range minus the warmup needed for the trigger.
    """
    dataset_dir = Path(dataset_dir)
    config = load_config(config_path)

    if isinstance(start_date, str):
        start_date = _parse_date(start_date)
    if isinstance(end_date, str):
        end_date = _parse_date(end_date)

    prices = _load_price_models(dataset_dir)
    events = _load_events(dataset_dir)
    fundamentals_timelines = _load_fundamentals_timelines(dataset_dir)

    us_securities: list[Security] = [
        s for s in config.securities
        if not s.ticker.endswith(".HK") and s.ticker in prices and prices[s.ticker]
    ]

    # Trading calendar = sorted union of all US security trading dates.
    calendar: list[date] = sorted({
        bar.trading_date for sec in us_securities for bar in prices[sec.ticker]
    })
    if not calendar:
        return []

    if start_date is None:
        # Skip the warmup period needed before any trigger can fire.
        start_index = min(_MIN_HISTORY_BARS, len(calendar) - 1)
        start_date = calendar[start_index]
    if end_date is None:
        end_date = calendar[-1]

    decision_dates = [d for d in calendar if start_date <= d <= end_date]

    statuses: list[dict] = []
    for run_date in decision_dates:
        contexts: list[PipelineContext] = []
        for sec in us_securities:
            sec_prices = [b for b in prices[sec.ticker] if b.trading_date <= run_date]
            if len(sec_prices) < _MIN_HISTORY_BARS:
                continue
            bench_prices = [
                b for b in prices.get(sec.benchmark, []) if b.trading_date <= run_date
            ]
            sec_events = [e for e in events.get(sec.ticker, []) if e.event_time.date() <= run_date]
            snapshot = snapshot_asof(fundamentals_timelines.get(sec.ticker, []), run_date)
            contexts.append(
                PipelineContext(
                    security=sec,
                    benchmark_ticker=sec.benchmark,
                    prices=sec_prices,
                    benchmark_prices=bench_prices,
                    official_events=sec_events,
                    fundamentals=snapshot,
                    macro=[],
                    source_statuses=list(_FULL_COVERAGE_STATUSES),
                )
            )

        layer_peers: dict = defaultdict(list)
        for ctx in contexts:
            layer_peers[ctx.security.layer].append(ctx)

        for ctx in contexts:
            threshold = config.thresholds[ctx.security.layer]
            trigger = compute_trigger(ctx.prices, ctx.benchmark_prices, threshold)
            if not trigger.triggered:
                continue
            peer_contexts = layer_peers[ctx.security.layer]
            event_tag = classify_event_tag(ctx, peer_contexts)
            scorecard = build_scorecard(
                run_date=run_date,
                context=ctx,
                trigger=trigger,
                event_tag=event_tag,
                peer_contexts=peer_contexts,
            )
            statuses.append({
                "date": run_date.isoformat(),
                "ticker": ctx.security.ticker,
                "layer": ctx.security.layer.value,
                "triggered": True,
                "state": scorecard.state.value,
                "total_score": scorecard.total_score,
                "veto_reason": scorecard.veto_reason,
                "data_insufficient": scorecard.data_insufficient,
                "event_tag": event_tag.value,
                "trigger_reasons": list(trigger.reasons),
                "close": round(ctx.prices[-1].close, 4),
                "buckets": {
                    "fundamentals": scorecard.fundamentals.score,
                    "sentiment": scorecard.sentiment.score,
                    "chain": scorecard.chain_confirmation.score,
                    "price_flow": scorecard.price_flow.score,
                    "risk": scorecard.risk_red_flags.score,
                    "social_rebound": scorecard.social_rebound.score,
                },
            })

    return statuses
