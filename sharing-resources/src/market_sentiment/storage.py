from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import shutil

from market_sentiment.models import FilingSummaryCacheRow, FundamentalSnapshot, MacroObservation, OfficialEvent, PriceBar, SocialPost, SocialPostCacheRow, SocialSnapshot
from market_sentiment.sources.social_base import SOCIAL_PROVIDER_NAMES


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

    def read_cached_prices(self, ticker: str, *, days_back: int = 60) -> list[PriceBar]:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days_back)).isoformat()
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

    def save_review_packets(self, run_date: date, packets: dict[str, dict]) -> list[Path]:
        packet_dir = self.data_dir / "reports" / run_date.isoformat() / "review_packets"
        packet_dir.mkdir(parents=True, exist_ok=True)
        for existing in packet_dir.glob("*.json"):
            existing.unlink()
        paths: list[Path] = []
        for ticker, packet in sorted(packets.items()):
            path = packet_dir / f"{ticker}.json"
            path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
            paths.append(path)
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
            conn.commit()

        return summary


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
