from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import shutil

from equity_research.models import AnalystConsensusSnapshotRow, AnalystRatingActionRow, FilingSummaryCacheRow, FundamentalSnapshot, MacroObservation, OfficialEvent, PriceBar, SocialPost, SocialPostCacheRow, SocialSnapshot
from equity_research.sources.social_base import SOCIAL_PROVIDER_NAMES


def _extract_tier2_from_full(full_packet: dict[str, Any]) -> dict[str, Any]:
    """Extract Tier 2 (working packet) from Tier 3 (full packet).

    Tier 2 removes:
    - price_context.recent_security_bars and recent_benchmark_bars (90+ bars)
    - Adds price_summary with compact metrics instead
    - Removes social_summary (large, optional, reader can fetch if needed)
    - Truncates fundamentals to history_summary only

    Note: Creates a deep copy to avoid modifying the original full_packet.
    """
    import copy

    # Start with a shallow copy to avoid modifying the original
    tier2 = {}

    # Copy all top-level keys except price_context and social_summary
    # Use deep copy for nested structures to avoid shared references
    for key in full_packet:
        if key in ("price_context", "social_summary"):
            continue
        # Deep copy to ensure we don't modify the original
        tier2[key] = copy.deepcopy(full_packet[key])

    # Build price_summary from price_context
    price_context = full_packet.get("price_context", {})
    price_summary = {}

    # Use latest bars to build summary metrics
    latest_sec = price_context.get("latest_security_bar")
    latest_bench = price_context.get("latest_benchmark_bar")
    recent_sec_bars = price_context.get("recent_security_bars", [])
    recent_bench_bars = price_context.get("recent_benchmark_bars", [])

    if latest_sec:
        price_summary["latest_close"] = latest_sec.get("close")
        price_summary["latest_date"] = latest_sec.get("trading_date")

    # Compute changes from bar arrays
    if len(recent_sec_bars) >= 2:
        price_summary["price_change_1d_pct"] = (
            (recent_sec_bars[-1]["close"] - recent_sec_bars[-2]["close"]) / recent_sec_bars[-2]["close"]
            if recent_sec_bars[-2]["close"] != 0
            else None
        )
    if len(recent_sec_bars) >= 5:
        price_summary["price_change_5d_pct"] = (
            (recent_sec_bars[-1]["close"] - recent_sec_bars[-5]["close"]) / recent_sec_bars[-5]["close"]
            if recent_sec_bars[-5]["close"] != 0
            else None
        )
    if len(recent_sec_bars) >= 20:
        price_summary["price_change_20d_pct"] = (
            (recent_sec_bars[-1]["close"] - recent_sec_bars[-20]["close"]) / recent_sec_bars[-20]["close"]
            if recent_sec_bars[-20]["close"] != 0
            else None
        )
    if len(recent_sec_bars) >= 60:
        price_summary["price_change_60d_pct"] = (
            (recent_sec_bars[-1]["close"] - recent_sec_bars[-60]["close"]) / recent_sec_bars[-60]["close"]
            if recent_sec_bars[-60]["close"] != 0
            else None
        )

    # Benchmark comparisons
    if recent_bench_bars and len(recent_sec_bars) >= 2:
        sec_1d = (recent_sec_bars[-1]["close"] - recent_sec_bars[-2]["close"]) / recent_sec_bars[-2]["close"] if recent_sec_bars[-2]["close"] != 0 else None
        bench_1d = (recent_bench_bars[-1]["close"] - recent_bench_bars[-2]["close"]) / recent_bench_bars[-2]["close"] if recent_bench_bars[-2]["close"] != 0 else None
        if sec_1d is not None and bench_1d is not None:
            price_summary["vs_benchmark_1d_pct"] = sec_1d - bench_1d

    # Moving average distances
    if len(recent_sec_bars) >= 20:
        sma_20 = sum(b["close"] for b in recent_sec_bars[-20:]) / 20
        price_summary["distance_from_20d_sma_pct"] = (recent_sec_bars[-1]["close"] - sma_20) / sma_20 if sma_20 != 0 else None
    if len(recent_sec_bars) >= 50:
        sma_50 = sum(b["close"] for b in recent_sec_bars[-50:]) / 50
        price_summary["distance_from_50d_sma_pct"] = (recent_sec_bars[-1]["close"] - sma_50) / sma_50 if sma_50 != 0 else None

    # Position in ranges
    if len(recent_sec_bars) >= 20:
        low_20 = min(b["low"] for b in recent_sec_bars[-20:])
        high_20 = max(b["high"] for b in recent_sec_bars[-20:])
        if high_20 != low_20:
            price_summary["position_in_20d_range"] = (recent_sec_bars[-1]["close"] - low_20) / (high_20 - low_20)

    tier2["price_summary"] = price_summary
    tier2["price_summary_note"] = "Compressed from 180 bars (90 security + 90 benchmark). Full bars in <ticker>.full.json"
    tier2["full_packet_path"] = f"{full_packet.get('security', {}).get('ticker', '?')}.full.json"

    # Simplify valuation_inputs in Tier 2
    vi = tier2.get("valuation_inputs")
    if vi and isinstance(vi, dict):
        # Remove the heavy fundamentals section, keep just a summary
        if "fundamentals" in vi:
            del vi["fundamentals"]
        # Add a note
        vi["fundamentals_note"] = "Full fundamentals (20 quarters of SEC concepts) in <ticker>.full.json"

    return tier2


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_date TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    triggered_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS source_payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_path TEXT,
    source_url TEXT,
    success INTEGER NOT NULL,
    partial INTEGER NOT NULL,
    message TEXT NOT NULL,
    ingested_at TEXT,
    event_start TEXT,
    event_end TEXT,
    decision_time TEXT
);

CREATE TABLE IF NOT EXISTS daily_prices (
    ticker TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    source TEXT NOT NULL,
    source_url TEXT,
    ingested_at TEXT,
    PRIMARY KEY (ticker, trading_date)
);

CREATE TABLE IF NOT EXISTS official_events (
    ticker TEXT NOT NULL,
    event_time TEXT NOT NULL,
    form_type TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    accepted_at TEXT,
    ingested_at TEXT,
    PRIMARY KEY (ticker, event_time, form_type, url)
);

CREATE TABLE IF NOT EXISTS fundamental_snapshots (
    ticker TEXT NOT NULL,
    period_end TEXT,
    filed_on TEXT,
    cik TEXT,
    revenue_latest REAL,
    revenue_previous REAL,
    operating_cashflow_latest REAL,
    operating_cashflow_previous REAL,
    capex_latest REAL,
    cash_latest REAL,
    debt_latest REAL,
    source TEXT NOT NULL,
    source_url TEXT,
    ingested_at TEXT,
    notes_json TEXT NOT NULL,
    PRIMARY KEY (ticker, period_end, source)
);

CREATE TABLE IF NOT EXISTS macro_observations (
    name TEXT NOT NULL,
    observed_on TEXT NOT NULL,
    value REAL NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT,
    ingested_at TEXT,
    PRIMARY KEY (name, observed_on, source)
);

CREATE TABLE IF NOT EXISTS social_posts (
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,
    community TEXT,
    post_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    url TEXT NOT NULL,
    author_handle TEXT,
    author_id_hash TEXT,
    engagement_score REAL NOT NULL,
    comment_count INTEGER,
    like_count INTEGER,
    repost_count INTEGER,
    language TEXT,
    is_repost INTEGER NOT NULL,
    matched_text INTEGER NOT NULL,
    stance INTEGER,
    quality_score REAL,
    themes_json TEXT NOT NULL,
    source_query TEXT,
    source_url TEXT,
    raw_payload_path TEXT,
    ingested_at TEXT,
    PRIMARY KEY (ticker, source, post_id)
);

CREATE TABLE IF NOT EXISTS social_snapshots (
    run_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,
    state TEXT NOT NULL,
    score INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_date, ticker, source)
);

CREATE TABLE IF NOT EXISTS scorecards (
    run_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    layer TEXT NOT NULL,
    event_tag TEXT NOT NULL,
    triggered INTEGER NOT NULL,
    total_score INTEGER NOT NULL,
    state TEXT NOT NULL,
    veto_reason TEXT,
    partial_coverage INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_date, ticker)
);

CREATE TABLE IF NOT EXISTS social_post_cache (
    source TEXT NOT NULL,
    post_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    title TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    confidence REAL NOT NULL,
    one_line_summary TEXT NOT NULL,
    engagement_score REAL NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (source, post_id, ticker)
);

CREATE INDEX IF NOT EXISTS idx_social_post_cache_ticker_posted_at
  ON social_post_cache(ticker, posted_at DESC);

CREATE TABLE IF NOT EXISTS filing_summary_cache (
    cik TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    ticker TEXT NOT NULL,
    form_type TEXT NOT NULL,
    filed_at TEXT NOT NULL,
    period_end TEXT,
    summary TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    key_metrics_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (cik, accession_number)
);

CREATE INDEX IF NOT EXISTS idx_filing_summary_cache_ticker_form_type
  ON filing_summary_cache(ticker, form_type);

CREATE TABLE IF NOT EXISTS analyst_consensus_snapshots (
    ticker TEXT NOT NULL,
    run_date TEXT NOT NULL,
    target_mean REAL,
    target_high REAL,
    target_low REAL,
    target_median REAL,
    number_of_analysts INTEGER,
    recommendation_key TEXT,
    recommendation_mean REAL,
    security_close REAL,
    source TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (ticker, run_date)
);

CREATE INDEX IF NOT EXISTS idx_analyst_consensus_snapshots_ticker_run_date
  ON analyst_consensus_snapshots(ticker, run_date DESC);

CREATE TABLE IF NOT EXISTS analyst_rating_actions (
    ticker TEXT NOT NULL,
    firm TEXT NOT NULL,
    action_date TEXT NOT NULL,
    action TEXT NOT NULL,
    from_grade TEXT,
    to_grade TEXT,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (ticker, firm, action_date, to_grade)
);

CREATE INDEX IF NOT EXISTS idx_analyst_rating_actions_ticker_action_date
  ON analyst_rating_actions(ticker, action_date DESC);

CREATE TABLE IF NOT EXISTS active_decisions (
    ticker TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    state TEXT NOT NULL,
    reference_close REAL NOT NULL,
    invalidate_conditions TEXT NOT NULL,
    rerate_conditions TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    last_checked_date TEXT,
    created_at TEXT NOT NULL,
    thesis_id TEXT,
    re_derivation_count INTEGER DEFAULT 1,
    resolution_close REAL,
    resolution_date TEXT,
    PRIMARY KEY (ticker, decision_date)
);

CREATE TABLE IF NOT EXISTS company_portraits (
    ticker TEXT NOT NULL,
    run_date TEXT NOT NULL,
    portrait_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (ticker, run_date)
);

CREATE INDEX IF NOT EXISTS idx_company_portraits_ticker_run_date
  ON company_portraits(ticker, run_date DESC);

CREATE TABLE IF NOT EXISTS model_orders (
    ticker TEXT NOT NULL,
    run_date TEXT NOT NULL,
    order_json TEXT NOT NULL,
    report_path TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (ticker, run_date)
);

CREATE INDEX IF NOT EXISTS idx_model_orders_ticker_run_date
  ON model_orders(ticker, run_date DESC);
"""


class Storage:
    def __init__(self, db_path: Path, data_dir: Path) -> None:
        self.db_path = db_path
        self.data_dir = data_dir
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def init_db(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(SCHEMA)
            _ensure_column(conn, "source_payloads", "source_url", "TEXT")
            _ensure_column(conn, "source_payloads", "ingested_at", "TEXT")
            _ensure_column(conn, "source_payloads", "event_start", "TEXT")
            _ensure_column(conn, "source_payloads", "event_end", "TEXT")
            _ensure_column(conn, "source_payloads", "decision_time", "TEXT")
            _ensure_column(conn, "daily_prices", "source_url", "TEXT")
            _ensure_column(conn, "daily_prices", "ingested_at", "TEXT")
            _ensure_column(conn, "official_events", "accepted_at", "TEXT")
            _ensure_column(conn, "official_events", "ingested_at", "TEXT")
            _ensure_column(conn, "macro_observations", "source_url", "TEXT")
            _ensure_column(conn, "macro_observations", "ingested_at", "TEXT")
            _ensure_column(conn, "active_decisions", "thesis_id", "TEXT")
            _ensure_column(conn, "active_decisions", "re_derivation_count", "INTEGER DEFAULT 1")
            _ensure_column(conn, "active_decisions", "resolution_close", "REAL")
            _ensure_column(conn, "active_decisions", "resolution_date", "TEXT")
            conn.commit()

    def raw_path(self, run_date: date, source: str, stem: str, suffix: str = "json") -> Path:
        directory = self.data_dir / "raw" / run_date.isoformat() / source
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{stem}.{suffix}"

    def write_raw_json(self, run_date: date, source: str, stem: str, payload: dict) -> Path:
        path = self.raw_path(run_date, source, stem, "json")
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def write_raw_text(self, run_date: date, source: str, stem: str, payload: str) -> Path:
        path = self.raw_path(run_date, source, stem, "txt")
        path.write_text(payload, encoding="utf-8")
        return path

    def upsert_prices(self, prices: list[PriceBar]) -> None:
        if not prices:
            return
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO daily_prices
                (ticker, trading_date, open, high, low, close, volume, source, source_url, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        price.ticker,
                        price.trading_date.isoformat(),
                        price.open,
                        price.high,
                        price.low,
                        price.close,
                        price.volume,
                        price.source,
                        price.source_url,
                        _dt(price.ingested_at),
                    )
                    for price in prices
                ],
            )
            conn.commit()

    def read_cached_prices(
        self, ticker: str, *, days_back: int = 60, reference_date: date | None = None
    ) -> list[PriceBar]:
        """Read cached bars for ``ticker`` from ``days_back`` days before ``reference_date``.

        ``reference_date`` defaults to today (UTC) when omitted, preserving prior
        behavior for existing callers. Pipeline callers computing a deep-history window
        relative to a specific run date (which may not be "today" — a delayed run, a
        test fixture) should pass ``reference_date=run_date`` explicitly;
        otherwise the cutoff is silently anchored to wall-clock "now" instead of the
        logical as-of date, which can exclude bars that are within depth of run_date but
        not within depth of today.
        """
        anchor = reference_date if reference_date is not None else datetime.now(timezone.utc).date()
        cutoff = (anchor - timedelta(days=days_back)).isoformat()
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT ticker, trading_date, open, high, low, close, volume,
                       source, source_url, ingested_at
                FROM daily_prices
                WHERE ticker = ? AND trading_date >= ?
                ORDER BY trading_date ASC
                """,
                (ticker, cutoff),
            ).fetchall()
        return [_row_to_price_bar(row) for row in rows]

    def get_earliest_cached_price_date(self, ticker: str) -> date | None:
        """Oldest ``trading_date`` cached for ``ticker``, or ``None`` if nothing is cached.

        Used to decide whether a ticker's cached price history is still shallow (e.g. a
        legacy pre-backfill database, or a brand-new ticker) and therefore needs a deep
        backfill, versus already covering the target depth and only needing an
        incremental top-up.
        """
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT MIN(trading_date) FROM daily_prices WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return date.fromisoformat(row[0])

    def get_latest_cached_price_date(self, ticker: str) -> date | None:
        """Newest ``trading_date`` cached for ``ticker``, or ``None`` if nothing is cached.

        Used to size an incremental fetch: only the gap between this date and the run
        date needs to be requested from the live sources, rather than re-pulling the
        full history.
        """
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT MAX(trading_date) FROM daily_prices WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return date.fromisoformat(row[0])

    def upsert_events(self, events: list[OfficialEvent]) -> None:
        if not events:
            return
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO official_events
                (ticker, event_time, form_type, title, url, source, accepted_at, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        event.ticker,
                        event.event_time.isoformat(),
                        event.form_type,
                        event.title,
                        event.url,
                        event.source,
                        _dt(event.accepted_at),
                        _dt(event.ingested_at),
                    )
                    for event in events
                ],
            )
            conn.commit()

    def upsert_fundamentals(self, snapshot: FundamentalSnapshot | None) -> None:
        if snapshot is None:
            return
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fundamental_snapshots
                (ticker, period_end, filed_on, cik, revenue_latest, revenue_previous,
                 operating_cashflow_latest, operating_cashflow_previous, capex_latest,
                 cash_latest, debt_latest, source, source_url, ingested_at, notes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.ticker,
                    _d(snapshot.period_end),
                    _d(snapshot.filed_on),
                    snapshot.cik,
                    snapshot.revenue_latest,
                    snapshot.revenue_previous,
                    snapshot.operating_cashflow_latest,
                    snapshot.operating_cashflow_previous,
                    snapshot.capex_latest,
                    snapshot.cash_latest,
                    snapshot.debt_latest,
                    snapshot.source,
                    snapshot.source_url,
                    _dt(snapshot.ingested_at),
                    json.dumps(snapshot.notes, sort_keys=True),
                ),
            )
            conn.commit()

    def upsert_macro(self, observations: list[MacroObservation]) -> None:
        if not observations:
            return
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO macro_observations
                (name, observed_on, value, source, source_url, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        observation.name,
                        observation.observed_on.isoformat(),
                        observation.value,
                        observation.source,
                        observation.source_url,
                        _dt(observation.ingested_at),
                    )
                    for observation in observations
                ],
            )
            conn.commit()

    def upsert_social_posts(self, posts: list[SocialPost]) -> None:
        if not posts:
            return
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO social_posts
                (ticker, source, community, post_id, created_at, title, body, url, author_handle,
                 author_id_hash, engagement_score, comment_count, like_count, repost_count, language,
                 is_repost, matched_text, stance, quality_score, themes_json, source_query, source_url,
                 raw_payload_path, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        post.ticker,
                        post.source,
                        post.community,
                        post.post_id,
                        post.created_at.isoformat(),
                        post.title,
                        post.body,
                        post.url,
                        post.author_handle,
                        post.author_id_hash,
                        post.engagement_score,
                        post.comment_count,
                        post.like_count,
                        post.repost_count,
                        post.language,
                        int(post.is_repost),
                        int(post.matched_text),
                        post.stance,
                        post.quality_score,
                        json.dumps(post.themes, sort_keys=True),
                        post.source_query,
                        post.source_url,
                        post.raw_payload_path,
                        _dt(post.ingested_at),
                    )
                    for post in posts
                ],
            )
            conn.commit()

    def upsert_social_snapshot(self, snapshot: SocialSnapshot | None, source: str = "social_rebound") -> None:
        if snapshot is None:
            return
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO social_snapshots
                (run_date, ticker, source, state, score, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.run_date.isoformat(),
                    snapshot.ticker,
                    source,
                    snapshot.state.value,
                    snapshot.score,
                    json.dumps(_snapshot_payload(snapshot), sort_keys=True),
                ),
            )
            conn.commit()

    def upsert_social_post_cache(self, posts: list[SocialPostCacheRow]) -> None:
        if not posts:
            return
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO social_post_cache
                (source, post_id, ticker, posted_at, title, sentiment, confidence,
                 one_line_summary, engagement_score, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        post.source,
                        post.post_id,
                        post.ticker,
                        post.posted_at.isoformat(),
                        post.title[:200],
                        post.sentiment,
                        post.confidence,
                        post.one_line_summary[:120],
                        post.engagement_score,
                        post.ingested_at.isoformat(),
                    )
                    for post in posts
                ],
            )
            conn.commit()

    def get_social_posts_for_ticker(self, ticker: str, since: datetime | None = None) -> list[SocialPostCacheRow]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            if since:
                rows = conn.execute(
                    """
                    SELECT source, post_id, ticker, posted_at, title, sentiment, confidence,
                           one_line_summary, engagement_score, ingested_at
                    FROM social_post_cache
                    WHERE ticker = ? AND posted_at >= ?
                    ORDER BY posted_at DESC
                    """,
                    (ticker, since.isoformat()),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT source, post_id, ticker, posted_at, title, sentiment, confidence,
                           one_line_summary, engagement_score, ingested_at
                    FROM social_post_cache
                    WHERE ticker = ?
                    ORDER BY posted_at DESC
                    """,
                    (ticker,),
                ).fetchall()
            return [_row_to_social_post_cache(row) for row in rows]

    def upsert_filing_summary_cache(self, filings: list[FilingSummaryCacheRow]) -> None:
        if not filings:
            return
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO filing_summary_cache
                (cik, accession_number, ticker, form_type, filed_at, period_end,
                 summary, sentiment, key_metrics_json, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        filing.cik,
                        filing.accession_number,
                        filing.ticker,
                        filing.form_type,
                        filing.filed_at.isoformat(),
                        filing.period_end.isoformat() if filing.period_end else None,
                        filing.summary,
                        filing.sentiment,
                        filing.key_metrics_json,
                        filing.ingested_at.isoformat(),
                    )
                    for filing in filings
                ],
            )
            conn.commit()

    def get_filing_summaries_for_ticker(
        self, ticker: str, form_types: list[str] | None = None
    ) -> list[FilingSummaryCacheRow]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            if form_types:
                placeholders = ",".join(["?"] * len(form_types))
                rows = conn.execute(
                    f"""
                    SELECT cik, accession_number, ticker, form_type, filed_at, period_end,
                           summary, sentiment, key_metrics_json, ingested_at
                    FROM filing_summary_cache
                    WHERE ticker = ? AND form_type IN ({placeholders})
                    ORDER BY filed_at DESC
                    """,
                    [ticker] + form_types,
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT cik, accession_number, ticker, form_type, filed_at, period_end,
                           summary, sentiment, key_metrics_json, ingested_at
                    FROM filing_summary_cache
                    WHERE ticker = ?
                    ORDER BY filed_at DESC
                    """,
                    (ticker,),
                ).fetchall()
            return [_row_to_filing_summary_cache(row) for row in rows]

    def purge_old_social_posts(self, cutoff: datetime) -> int:
        with closing(sqlite3.connect(self.db_path)) as conn:
            rowcount = conn.execute(
                "DELETE FROM social_post_cache WHERE ingested_at < ?",
                (cutoff.isoformat(),),
            ).rowcount
            conn.commit()
            return rowcount

    def purge_old_filing_summaries(self, cutoff: datetime) -> int:
        with closing(sqlite3.connect(self.db_path)) as conn:
            rowcount = conn.execute(
                "DELETE FROM filing_summary_cache WHERE ingested_at < ?",
                (cutoff.isoformat(),),
            ).rowcount
            conn.commit()
            return rowcount

    def upsert_analyst_consensus_snapshot(self, row: AnalystConsensusSnapshotRow) -> None:
        """Idempotent on (ticker, run_date): re-running a day overwrites, not duplicates."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO analyst_consensus_snapshots
                (ticker, run_date, target_mean, target_high, target_low, target_median,
                 number_of_analysts, recommendation_key, recommendation_mean, security_close,
                 source, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.ticker,
                    row.run_date.isoformat(),
                    row.target_mean,
                    row.target_high,
                    row.target_low,
                    row.target_median,
                    row.number_of_analysts,
                    row.recommendation_key,
                    row.recommendation_mean,
                    row.security_close,
                    row.source,
                    row.ingested_at.isoformat(),
                ),
            )
            conn.commit()

    def get_analyst_consensus_history(self, ticker: str) -> list[AnalystConsensusSnapshotRow]:
        """Full stored consensus-snapshot history for a ticker, ascending by run_date."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT ticker, run_date, target_mean, target_high, target_low, target_median,
                       number_of_analysts, recommendation_key, recommendation_mean, security_close,
                       source, ingested_at
                FROM analyst_consensus_snapshots
                WHERE ticker = ?
                ORDER BY run_date ASC
                """,
                (ticker,),
            ).fetchall()
        return [_row_to_analyst_consensus_snapshot(row) for row in rows]

    def upsert_analyst_rating_actions(self, rows: list[AnalystRatingActionRow]) -> None:
        """Deduplicated on (ticker, firm, action_date, to_grade) via INSERT OR IGNORE."""
        if not rows:
            return
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO analyst_rating_actions
                (ticker, firm, action_date, action, from_grade, to_grade, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row.ticker,
                        row.firm,
                        row.action_date.isoformat(),
                        row.action,
                        row.from_grade,
                        row.to_grade,
                        row.ingested_at.isoformat(),
                    )
                    for row in rows
                ],
            )
            conn.commit()

    def get_analyst_rating_actions(
        self, ticker: str, since: date | None = None
    ) -> list[AnalystRatingActionRow]:
        """Full stored rating-action history for a ticker, ascending by action_date."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            if since is not None:
                rows = conn.execute(
                    """
                    SELECT ticker, firm, action_date, action, from_grade, to_grade, ingested_at
                    FROM analyst_rating_actions
                    WHERE ticker = ? AND action_date >= ?
                    ORDER BY action_date ASC
                    """,
                    (ticker, since.isoformat()),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT ticker, firm, action_date, action, from_grade, to_grade, ingested_at
                    FROM analyst_rating_actions
                    WHERE ticker = ?
                    ORDER BY action_date ASC
                    """,
                    (ticker,),
                ).fetchall()
        return [_row_to_analyst_rating_action(row) for row in rows]

    def get_price_on_or_before(self, ticker: str, as_of: date) -> tuple[date, float] | None:
        """Latest (trading_date, close) with trading_date <= as_of, or None."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT trading_date, close
                FROM daily_prices
                WHERE ticker = ? AND trading_date <= ?
                ORDER BY trading_date DESC
                LIMIT 1
                """,
                (ticker, as_of.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return date.fromisoformat(row[0]), row[1]

    def upsert_active_decision(
        self,
        *,
        ticker: str,
        decision_date: str,
        state: str,
        reference_close: float,
        invalidate_conditions: list,
        rerate_conditions: list,
        status: str = "active",
        status_reason: str | None = None,
        last_checked_date: str | None = None,
        thesis_id: str | None = None,
        re_derivation_count: int | None = None,
        resolution_close: float | None = None,
        resolution_date: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            # Generate thesis_id if not provided and status is active
            if status == "active" and thesis_id is None:
                import uuid
                thesis_id = str(uuid.uuid4())

            # Set re_derivation_count default
            if re_derivation_count is None:
                re_derivation_count = 1

            conn.execute(
                """
                INSERT OR REPLACE INTO active_decisions
                (ticker, decision_date, state, reference_close, invalidate_conditions,
                 rerate_conditions, status, status_reason, last_checked_date, created_at,
                 thesis_id, re_derivation_count, resolution_close, resolution_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker,
                    decision_date,
                    state,
                    reference_close,
                    json.dumps(invalidate_conditions, sort_keys=True),
                    json.dumps(rerate_conditions, sort_keys=True),
                    status,
                    status_reason,
                    last_checked_date,
                    datetime.now(timezone.utc).isoformat(),
                    thesis_id,
                    re_derivation_count,
                    resolution_close,
                    resolution_date,
                ),
            )
            conn.commit()

    def read_active_decisions(self) -> list[dict]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT ticker, decision_date, state, reference_close, invalidate_conditions,
                       rerate_conditions, status, status_reason, last_checked_date, created_at,
                       thesis_id, re_derivation_count, resolution_close, resolution_date
                FROM active_decisions
                WHERE status = 'active'
                """
            ).fetchall()
        return [
            {
                "ticker": row[0],
                "decision_date": row[1],
                "state": row[2],
                "reference_close": row[3],
                "invalidate_conditions": json.loads(row[4]),
                "rerate_conditions": json.loads(row[5]),
                "status": row[6],
                "status_reason": row[7],
                "last_checked_date": row[8],
                "created_at": row[9],
                "thesis_id": row[10],
                "re_derivation_count": row[11],
                "resolution_close": row[12],
                "resolution_date": row[13],
            }
            for row in rows
        ]

    def read_all_decisions_for_ticker(self, ticker: str) -> list[dict]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT ticker, decision_date, state, reference_close, invalidate_conditions,
                       rerate_conditions, status, status_reason, last_checked_date, created_at,
                       thesis_id, re_derivation_count, resolution_close, resolution_date
                FROM active_decisions
                WHERE ticker = ?
                ORDER BY decision_date DESC
                """,
                (ticker,),
            ).fetchall()
        return [
            {
                "ticker": row[0],
                "decision_date": row[1],
                "state": row[2],
                "reference_close": row[3],
                "invalidate_conditions": json.loads(row[4]),
                "rerate_conditions": json.loads(row[5]),
                "status": row[6],
                "status_reason": row[7],
                "last_checked_date": row[8],
                "created_at": row[9],
                "thesis_id": row[10],
                "re_derivation_count": row[11],
                "resolution_close": row[12],
                "resolution_date": row[13],
            }
            for row in rows
        ]

    def update_decision_status(
        self,
        *,
        ticker: str,
        decision_date: str,
        status: str,
        status_reason: str | None,
        last_checked_date: str,
        resolution_close: float | None = None,
        re_derivation_count: int | None = None,
    ) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            # Build the update query dynamically based on what's provided
            updates = ["status = ?", "status_reason = ?", "last_checked_date = ?"]
            params = [status, status_reason, last_checked_date]

            if resolution_close is not None:
                updates.append("resolution_close = ?")
                params.append(resolution_close)
                # When closing, also set resolution_date
                updates.append("resolution_date = ?")
                params.append(last_checked_date)

            if re_derivation_count is not None:
                updates.append("re_derivation_count = ?")
                params.append(re_derivation_count)

            params.extend([ticker, decision_date])

            conn.execute(
                f"""
                UPDATE active_decisions
                SET {", ".join(updates)}
                WHERE ticker = ? AND decision_date = ?
                """,
                params,
            )
            conn.commit()

    def save_review_packets(self, run_date: date, packets: dict[str, dict]) -> list[Path]:
        """Save review packets as three tiers: delta, working (Tier 2), and full.

        Returns paths to all saved files (delta, tier2, and full for each ticker).
        """
        packet_dir = self.data_dir / "reports" / run_date.isoformat() / "review_packets"
        packet_dir.mkdir(parents=True, exist_ok=True)

        # Clean up old files (all tiers)
        for existing in packet_dir.glob("*.json"):
            existing.unlink()

        paths: list[Path] = []
        for ticker, full_packet in sorted(packets.items()):
            # Tier 3: Save full packet
            full_path = packet_dir / f"{ticker}.full.json"
            full_path.write_text(json.dumps(full_packet, indent=2, sort_keys=True), encoding="utf-8")
            paths.append(full_path)

            # Tier 2: Save working packet (this is what agents load by default)
            # We'll extract it from the full packet or build a simplified version
            tier2_packet = _extract_tier2_from_full(full_packet)
            tier2_path = packet_dir / f"{ticker}.json"
            tier2_path.write_text(json.dumps(tier2_packet, indent=2, sort_keys=True), encoding="utf-8")
            paths.append(tier2_path)

            # Tier 1: Delta packet (try to build, may be None if no previous run)
            # This would be built in the pipeline before saving, so we skip it here
            # The delta packet is optional and built only if there's a previous run

        return paths

    def save_manual_agent_report(self, run_date: date, content: str) -> Path:
        path = self.data_dir / "reports" / run_date.isoformat() / "manual_agent_report.zh.md"
        path.write_text(content, encoding="utf-8")
        return path

    def load_delivery_report_markdown(self, run_date: date) -> str:
        path = self.data_dir / "reports" / run_date.isoformat() / "manual_agent_report.zh.md"
        return path.read_text(encoding="utf-8")

    def cleanup_retention(
        self,
        reference_date: date,
        report_days: int,
        raw_payload_days: int,
        daily_price_days: int,
        official_event_days: int,
        fundamental_days: int,
        macro_days: int,
        run_metadata_days: int,
        social_raw_payload_days: int | None = None,
        social_post_days: int | None = None,
        social_snapshot_days: int | None = None,
    ) -> "CleanupSummary":
        self.init_db()
        summary = CleanupSummary(reference_date=reference_date)
        summary.deleted_report_dirs = _cleanup_dated_directories(
            self.data_dir / "reports",
            keep_days=report_days,
            reference_date=reference_date,
        )
        summary.deleted_raw_dirs = _cleanup_raw_directories(
            self.data_dir / "raw",
            keep_days=raw_payload_days,
            social_keep_days=social_raw_payload_days if social_raw_payload_days is not None else raw_payload_days,
            reference_date=reference_date,
        )

        run_cutoff = _cutoff_date(reference_date, run_metadata_days).isoformat()
        price_cutoff = _cutoff_date(reference_date, daily_price_days).isoformat()
        event_cutoff = _cutoff_date(reference_date, official_event_days).isoformat()
        fundamental_cutoff = _cutoff_date(reference_date, fundamental_days).isoformat()
        macro_cutoff = _cutoff_date(reference_date, macro_days).isoformat()
        social_post_cutoff = _cutoff_date(
            reference_date,
            social_post_days if social_post_days is not None else official_event_days,
        ).isoformat()
        social_snapshot_cutoff = _cutoff_date(
            reference_date,
            social_snapshot_days if social_snapshot_days is not None else run_metadata_days,
        ).isoformat()

        with closing(sqlite3.connect(self.db_path)) as conn:
            summary.deleted_db_rows["source_payloads"] = conn.execute(
                "DELETE FROM source_payloads WHERE run_date < ?",
                (run_cutoff,),
            ).rowcount
            summary.deleted_db_rows["scorecards"] = conn.execute(
                "DELETE FROM scorecards WHERE run_date < ?",
                (run_cutoff,),
            ).rowcount
            summary.deleted_db_rows["runs"] = conn.execute(
                "DELETE FROM runs WHERE run_date < ?",
                (run_cutoff,),
            ).rowcount
            summary.deleted_db_rows["daily_prices"] = conn.execute(
                "DELETE FROM daily_prices WHERE trading_date < ?",
                (price_cutoff,),
            ).rowcount
            summary.deleted_db_rows["official_events"] = conn.execute(
                "DELETE FROM official_events WHERE substr(event_time, 1, 10) < ?",
                (event_cutoff,),
            ).rowcount
            summary.deleted_db_rows["fundamental_snapshots"] = conn.execute(
                """
                DELETE FROM fundamental_snapshots
                WHERE COALESCE(filed_on, period_end) IS NOT NULL
                  AND COALESCE(filed_on, period_end) < ?
                """,
                (fundamental_cutoff,),
            ).rowcount
            summary.deleted_db_rows["macro_observations"] = conn.execute(
                "DELETE FROM macro_observations WHERE observed_on < ?",
                (macro_cutoff,),
            ).rowcount
            summary.deleted_db_rows["social_posts"] = conn.execute(
                "DELETE FROM social_posts WHERE substr(created_at, 1, 10) < ?",
                (social_post_cutoff,),
            ).rowcount
            summary.deleted_db_rows["social_snapshots"] = conn.execute(
                "DELETE FROM social_snapshots WHERE run_date < ?",
                (social_snapshot_cutoff,),
            ).rowcount
            decision_cutoff = _cutoff_date(reference_date, 180).isoformat()
            summary.deleted_db_rows["active_decisions"] = conn.execute(
                """
                DELETE FROM active_decisions
                WHERE status != 'active' AND created_at < ?
                """,
                (decision_cutoff,),
            ).rowcount
            conn.commit()

        return summary

    def upsert_company_portrait(self, ticker: str, run_date: date, portrait_json: str) -> None:
        """Store or update a company portrait (agent's answers to six portrait questions)."""
        created_at = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO company_portraits
                (ticker, run_date, portrait_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (ticker, run_date.isoformat(), portrait_json, created_at),
            )
            conn.commit()

    def get_latest_company_portrait(self, ticker: str, exclude_run_date: date | None = None) -> dict[str, Any] | None:
        """Retrieve the most recent company portrait for a ticker, optionally excluding a specific run date.

        Returns a dict with 'portrait_json', 'run_date', and 'days_ago', or None if no portrait exists.
        """
        with closing(sqlite3.connect(self.db_path)) as conn:
            if exclude_run_date:
                row = conn.execute(
                    """
                    SELECT portrait_json, run_date
                    FROM company_portraits
                    WHERE ticker = ? AND run_date != ?
                    ORDER BY run_date DESC
                    LIMIT 1
                    """,
                    (ticker, exclude_run_date.isoformat()),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT portrait_json, run_date
                    FROM company_portraits
                    WHERE ticker = ?
                    ORDER BY run_date DESC
                    LIMIT 1
                    """,
                    (ticker,),
                ).fetchone()

        if row is None:
            return None

        portrait_json_str, run_date_str = row
        portrait_run_date = date.fromisoformat(run_date_str)
        days_ago = (exclude_run_date if exclude_run_date else date.today()) - portrait_run_date
        return {
            "portrait_json": portrait_json_str,
            "run_date": portrait_run_date.isoformat(),
            "days_ago": days_ago.days,
        }

    def upsert_model_order(self, ticker: str, run_date: date, order_json: str, report_path: str | None = None) -> None:
        """Store or update a model order and its resulting report location."""
        created_at = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO model_orders
                (ticker, run_date, order_json, report_path, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ticker, run_date.isoformat(), order_json, report_path, created_at),
            )
            conn.commit()

    def get_latest_model_order(self, ticker: str, exclude_run_date: date | None = None) -> dict[str, Any] | None:
        """Retrieve the most recent model order for a ticker, optionally excluding a specific run date.

        Returns a dict with 'order_json', 'report_path', 'run_date', and 'days_ago', or None if no order exists.
        """
        with closing(sqlite3.connect(self.db_path)) as conn:
            if exclude_run_date:
                row = conn.execute(
                    """
                    SELECT order_json, report_path, run_date
                    FROM model_orders
                    WHERE ticker = ? AND run_date != ?
                    ORDER BY run_date DESC
                    LIMIT 1
                    """,
                    (ticker, exclude_run_date.isoformat()),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT order_json, report_path, run_date
                    FROM model_orders
                    WHERE ticker = ?
                    ORDER BY run_date DESC
                    LIMIT 1
                    """,
                    (ticker,),
                ).fetchone()

        if row is None:
            return None

        order_json_str, report_path, run_date_str = row
        order_run_date = date.fromisoformat(run_date_str)
        days_ago = (exclude_run_date if exclude_run_date else date.today()) - order_run_date
        return {
            "order_json": order_json_str,
            "report_path": report_path,
            "run_date": order_run_date.isoformat(),
            "days_ago": days_ago.days,
        }


@dataclass(slots=True)
class CleanupSummary:
    reference_date: date
    deleted_report_dirs: list[str] = field(default_factory=list)
    deleted_raw_dirs: list[str] = field(default_factory=list)
    deleted_db_rows: dict[str, int] = field(default_factory=dict)

    def to_lines(self) -> list[str]:
        return [
            f"Cleanup reference date: {self.reference_date.isoformat()}",
            f"Deleted report directories: {len(self.deleted_report_dirs)}",
            f"Deleted raw directories: {len(self.deleted_raw_dirs)}",
            "Deleted DB rows:",
            *[
                f"- {table}: {count}"
                for table, count in sorted(self.deleted_db_rows.items())
            ],
        ]


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _d(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    existing = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if column not in existing:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _cutoff_date(reference_date: date, keep_days: int) -> date:
    return reference_date - timedelta(days=max(0, keep_days))


def _snapshot_payload(snapshot: SocialSnapshot) -> dict:
    return {
        "ticker": snapshot.ticker,
        "run_date": snapshot.run_date.isoformat(),
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
        "state": snapshot.state.value,
        "score": snapshot.score,
        "provider_post_counts": snapshot.provider_post_counts,
        "recent_provider_post_counts": snapshot.recent_provider_post_counts,
        "source_summaries": [
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
                "top_bullish_themes": [
                    {"label": theme.label, "direction": theme.direction, "count": theme.count}
                    for theme in source_summary.top_bullish_themes
                ],
                "top_bearish_themes": [
                    {"label": theme.label, "direction": theme.direction, "count": theme.count}
                    for theme in source_summary.top_bearish_themes
                ],
                "representative_posts": [post.post_id for post in source_summary.representative_posts],
            }
            for source_summary in snapshot.source_summaries
        ],
        "notes": snapshot.notes,
        "top_bullish_themes": [
            {"label": theme.label, "direction": theme.direction, "count": theme.count}
            for theme in snapshot.top_bullish_themes
        ],
        "top_bearish_themes": [
            {"label": theme.label, "direction": theme.direction, "count": theme.count}
            for theme in snapshot.top_bearish_themes
        ],
        "representative_posts": [post.post_id for post in snapshot.representative_posts],
    }


def _cleanup_dated_directories(base_dir: Path, keep_days: int, reference_date: date) -> list[str]:
    if not base_dir.exists():
        return []
    cutoff = _cutoff_date(reference_date, keep_days)
    deleted: list[str] = []
    for child in base_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            child_date = date.fromisoformat(child.name)
        except ValueError:
            continue
        if child_date < cutoff:
            shutil.rmtree(child)
            deleted.append(str(child))
    return sorted(deleted)


def _cleanup_raw_directories(
    base_dir: Path,
    *,
    keep_days: int,
    social_keep_days: int,
    reference_date: date,
) -> list[str]:
    if not base_dir.exists():
        return []
    default_cutoff = _cutoff_date(reference_date, keep_days)
    social_cutoff = _cutoff_date(reference_date, social_keep_days)
    social_sources = set(SOCIAL_PROVIDER_NAMES)
    deleted: list[str] = []

    for date_dir in base_dir.iterdir():
        if not date_dir.is_dir():
            continue
        try:
            dir_date = date.fromisoformat(date_dir.name)
        except ValueError:
            continue
        for source_dir in list(date_dir.iterdir()):
            if not source_dir.is_dir():
                continue
            cutoff = social_cutoff if source_dir.name in social_sources else default_cutoff
            if dir_date < cutoff:
                shutil.rmtree(source_dir)
                deleted.append(str(source_dir))
        if date_dir.exists() and not any(date_dir.iterdir()):
            shutil.rmtree(date_dir)
            deleted.append(str(date_dir))

    return sorted(deleted)


def _row_to_price_bar(row: tuple) -> PriceBar:
    return PriceBar(
        ticker=row[0],
        trading_date=date.fromisoformat(row[1]),
        open=row[2],
        high=row[3],
        low=row[4],
        close=row[5],
        volume=row[6],
        source=row[7],
        source_url=row[8],
        ingested_at=datetime.fromisoformat(row[9]) if row[9] else None,
    )


def _row_to_social_post_cache(row: tuple) -> SocialPostCacheRow:
    return SocialPostCacheRow(
        source=row[0],
        post_id=row[1],
        ticker=row[2],
        posted_at=datetime.fromisoformat(row[3]),
        title=row[4],
        sentiment=row[5],
        confidence=row[6],
        one_line_summary=row[7],
        engagement_score=row[8],
        ingested_at=datetime.fromisoformat(row[9]),
    )


def _row_to_analyst_consensus_snapshot(row: tuple) -> AnalystConsensusSnapshotRow:
    return AnalystConsensusSnapshotRow(
        ticker=row[0],
        run_date=date.fromisoformat(row[1]),
        target_mean=row[2],
        target_high=row[3],
        target_low=row[4],
        target_median=row[5],
        number_of_analysts=row[6],
        recommendation_key=row[7],
        recommendation_mean=row[8],
        security_close=row[9],
        source=row[10],
        ingested_at=datetime.fromisoformat(row[11]),
    )


def _row_to_analyst_rating_action(row: tuple) -> AnalystRatingActionRow:
    return AnalystRatingActionRow(
        ticker=row[0],
        firm=row[1],
        action_date=date.fromisoformat(row[2]),
        action=row[3],
        from_grade=row[4] or "",
        to_grade=row[5] or "",
        ingested_at=datetime.fromisoformat(row[6]),
    )


def _row_to_filing_summary_cache(row: tuple) -> FilingSummaryCacheRow:
    period_end = datetime.fromisoformat(row[5]) if row[5] else None
    return FilingSummaryCacheRow(
        cik=row[0],
        accession_number=row[1],
        ticker=row[2],
        form_type=row[3],
        filed_at=datetime.fromisoformat(row[4]),
        period_end=period_end,
        summary=row[6],
        sentiment=row[7],
        key_metrics_json=row[8],
        ingested_at=datetime.fromisoformat(row[9]),
    )
