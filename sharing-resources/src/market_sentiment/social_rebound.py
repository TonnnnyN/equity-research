from __future__ import annotations
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from market_sentiment.models import SocialPost, SocialReboundState, SocialSnapshot, SocialSourceSummary, SocialThemeSummary
from market_sentiment.sources.social_base import dedupe_social_posts


BULLISH_KEYWORDS = {
    "beat",
    "beats",
    "strong",
    "improving",
    "improved",
    "rebound",
    "stabilizing",
    "stabilised",
    "healthy",
    "backlog",
    "demand",
    "margin",
    "margins",
    "cash flow",
    "undervalued",
    "upgrade",
    "acceleration",
    "accelerating",
    "order",
    "orders",
}

BEARISH_KEYWORDS = {
    "miss",
    "missed",
    "weak",
    "weaker",
    "slowing",
    "slowdown",
    "downgrade",
    "cut",
    "cuts",
    "sell",
    "panic",
    "bearish",
    "overvalued",
    "decline",
    "dilution",
    "liquidity",
    "guidance cut",
}

HARD_NEGATIVE_KEYWORDS = {
    "fraud",
    "bankruptcy",
    "guidance cut",
    "investigation",
    "accounting",
    "liquidity",
    "dilution",
    "default",
    "restatement",
}

THEME_KEYWORDS = {
    "demand": {"demand", "backlog", "order", "orders"},
    "earnings": {"earnings", "quarter", "guidance", "results"},
    "cash_flow": {"cash flow", "fcf", "operating cash flow"},
    "valuation": {"undervalued", "overvalued", "multiple", "valuation"},
    "governance_risk": {"fraud", "accounting", "investigation", "restatement"},
}

LOW_SIGNAL_PATTERNS = {
    "to the moon",
    "all in",
    "diamond hands",
    "rocket",
    "bagholder",
    "paper hands",
}


def build_social_snapshot(
    *,
    ticker: str,
    run_date: date,
    posts: list[SocialPost],
    min_informative_posts: int,
    min_unique_authors: int,
    min_sources: int,
    max_author_share: float,
    recent_window_hours: int,
    baseline_days: int,
    provider_names: set[str],
    timezone_name: str = "UTC",
) -> SocialSnapshot:
    run_zone = _resolve_timezone(timezone_name)
    run_cutoff = datetime.combine(run_date, time.max, tzinfo=run_zone).astimezone(timezone.utc)
    recent_cutoff = run_cutoff - timedelta(hours=recent_window_hours)
    baseline_cutoff = recent_cutoff - timedelta(days=baseline_days)

    deduped = dedupe_social_posts(posts)
    informative = [post for post in deduped if _is_informative(post)]
    recent_posts = [post for post in informative if recent_cutoff <= _to_utc(post.created_at) <= run_cutoff]
    baseline_posts = [post for post in informative if baseline_cutoff <= _to_utc(post.created_at) < recent_cutoff]
    providers = {post.source for post in informative if post.source}
    communities = {post.community for post in informative if post.community}
    provider_post_counts = _count_by_provider(informative)
    recent_provider_post_counts = _count_by_provider(recent_posts)
    unique_authors = {
        post.author_id_hash or post.author_handle or f"anon:{post.post_id}"
        for post in recent_posts
    }
    author_concentration = _author_concentration(recent_posts)
    recent_stance = _stance_score(recent_posts)
    baseline_stance = _stance_score(baseline_posts)
    delta = recent_stance - baseline_stance
    breadth = _breadth_score(
        recent_posts=recent_posts,
        unique_authors=len(unique_authors),
        provider_count=len(provider_names),
        min_informative_posts=min_informative_posts,
        min_unique_authors=min_unique_authors,
        min_sources=min_sources,
    )
    hard_negative_ratio = _hard_negative_ratio(recent_posts)
    notes: list[str] = []

    if len(recent_posts) < min_informative_posts:
        notes.append("social_recent_sample_insufficient")
    if len(unique_authors) < min_unique_authors:
        notes.append("social_author_sample_insufficient")
    if len(provider_names) < min_sources:
        notes.append("social_source_count_insufficient")
    if author_concentration > max_author_share:
        notes.append("social_author_concentration_high")
    if hard_negative_ratio >= 0.35:
        notes.append("social_hard_negative_topics_dominant")

    state = SocialReboundState.INSUFFICIENT
    score = 0
    sample_ready = (
        len(recent_posts) >= min_informative_posts
        and len(unique_authors) >= min_unique_authors
        and len(provider_names) >= min_sources
        and author_concentration <= max_author_share
    )
    if sample_ready:
        if hard_negative_ratio >= 0.35:
            state = SocialReboundState.WORSENING
            score = -6
        elif delta >= 0.20 and breadth >= 0.60:
            state = SocialReboundState.STRONG_REBOUND
            score = 8
        elif delta >= 0.10 and breadth >= 0.40:
            state = SocialReboundState.MILD_REBOUND
            score = 4
        elif delta <= -0.10:
            state = SocialReboundState.WORSENING
            score = -4
        else:
            state = SocialReboundState.FLAT
            score = 0

    return SocialSnapshot(
        ticker=ticker,
        run_date=run_date,
        recent_window_hours=recent_window_hours,
        baseline_days=baseline_days,
        provider_count=len(provider_names),
        community_count=len(communities),
        total_posts=len(posts),
        informative_posts=len(informative),
        recent_posts=len(recent_posts),
        baseline_posts=len(baseline_posts),
        unique_authors=len(unique_authors),
        author_concentration=author_concentration,
        recent_stance=recent_stance,
        baseline_stance=baseline_stance,
        delta=delta,
        breadth=breadth,
        hard_negative_ratio=hard_negative_ratio,
        state=state,
        score=score,
        provider_post_counts=provider_post_counts,
        recent_provider_post_counts=recent_provider_post_counts,
        source_summaries=_build_source_summaries(
            posts=deduped,
            informative_posts=informative,
            recent_posts=recent_posts,
            baseline_posts=baseline_posts,
        ),
        notes=notes,
        top_bullish_themes=_top_themes(recent_posts, positive=True),
        top_bearish_themes=_top_themes(recent_posts, positive=False),
        representative_posts=_representative_posts(recent_posts),
    )


def annotate_posts(posts: list[SocialPost]) -> list[SocialPost]:
    annotated: list[SocialPost] = []
    for post in posts:
        text = _post_text(post)
        post.stance = _classify_stance(text)
        post.quality_score = _quality_score(post, text)
        post.themes = _extract_themes(text)
        annotated.append(post)
    return annotated


def _post_text(post: SocialPost) -> str:
    return f"{post.title}\n{post.body}".strip().lower()


def _classify_stance(text: str) -> int:
    bullish = sum(1 for keyword in BULLISH_KEYWORDS if keyword in text)
    bearish = sum(1 for keyword in BEARISH_KEYWORDS if keyword in text)
    if bullish >= bearish + 1:
        return 1
    if bearish >= bullish + 1:
        return -1
    return 0


def _quality_score(post: SocialPost, text: str) -> float:
    score = 0.25
    if len(text) >= 80:
        score += 0.30
    if any(keyword in text for keywords in THEME_KEYWORDS.values() for keyword in keywords):
        score += 0.20
    if post.engagement_score >= 20:
        score += 0.15
    if not any(pattern in text for pattern in LOW_SIGNAL_PATTERNS):
        score += 0.10
    return min(score, 1.0)


def _extract_themes(text: str) -> list[str]:
    themes = [label for label, keywords in THEME_KEYWORDS.items() if any(keyword in text for keyword in keywords)]
    return themes[:4]
def _is_informative(post: SocialPost) -> bool:
    text = _post_text(post)
    if len(text) < 20:
        return False
    if any(pattern in text for pattern in LOW_SIGNAL_PATTERNS):
        return False
    if post.stance is None or post.quality_score is None:
        post.stance = _classify_stance(text)
        post.quality_score = _quality_score(post, text)
        post.themes = _extract_themes(text)
    return bool(post.matched_text) and (post.quality_score or 0.0) >= 0.35


def _stance_score(posts: list[SocialPost]) -> float:
    weighted_total = 0.0
    total_weight = 0.0
    for post in posts:
        weight = _post_weight(post)
        weighted_total += weight * float(post.stance or 0)
        total_weight += weight
    if total_weight == 0:
        return 0.0
    return weighted_total / total_weight


def _post_weight(post: SocialPost) -> float:
    base = 1.0 + min(post.engagement_score, 50.0) / 50.0
    quality = post.quality_score if post.quality_score is not None else 0.5
    return base * max(0.2, quality)


def _breadth_score(
    *,
    recent_posts: list[SocialPost],
    unique_authors: int,
    provider_count: int,
    min_informative_posts: int,
    min_unique_authors: int,
    min_sources: int,
) -> float:
    post_term = min(1.0, len(recent_posts) / max(min_informative_posts, 1))
    author_term = min(1.0, unique_authors / max(min_unique_authors, 1))
    source_term = min(1.0, provider_count / max(min_sources, 1))
    return round(post_term * author_term * source_term, 4)


def _author_concentration(posts: list[SocialPost]) -> float:
    if not posts:
        return 0.0
    counts = Counter(post.author_id_hash or post.author_handle or f"anon:{post.post_id}" for post in posts)
    return max(counts.values()) / len(posts)


def _hard_negative_ratio(posts: list[SocialPost]) -> float:
    if not posts:
        return 0.0
    hard_negative_count = 0
    for post in posts:
        text = _post_text(post)
        if any(keyword in text for keyword in HARD_NEGATIVE_KEYWORDS):
            hard_negative_count += 1
    return hard_negative_count / len(posts)


def _top_themes(posts: list[SocialPost], *, positive: bool) -> list[SocialThemeSummary]:
    counter: Counter[str] = Counter()
    for post in posts:
        if positive and (post.stance or 0) <= 0:
            continue
        if not positive and (post.stance or 0) >= 0:
            continue
        counter.update(post.themes)
    return [
        SocialThemeSummary(
            label=label,
            direction="positive" if positive else "negative",
            count=count,
        )
        for label, count in counter.most_common(3)
    ]


def _representative_posts(posts: list[SocialPost]) -> list[SocialPost]:
    ranked = sorted(posts, key=lambda item: ((item.quality_score or 0.0), item.engagement_score), reverse=True)
    return ranked[:3]


def _count_by_provider(posts: list[SocialPost]) -> dict[str, int]:
    counter = Counter(post.source or "unknown" for post in posts)
    return dict(sorted(counter.items()))


def _build_source_summaries(
    *,
    posts: list[SocialPost],
    informative_posts: list[SocialPost],
    recent_posts: list[SocialPost],
    baseline_posts: list[SocialPost],
) -> list[SocialSourceSummary]:
    sources = sorted({post.source or "unknown" for post in posts})
    summaries: list[SocialSourceSummary] = []
    for source in sources:
        source_posts = [post for post in posts if (post.source or "unknown") == source]
        source_informative = [post for post in informative_posts if (post.source or "unknown") == source]
        source_recent = [post for post in recent_posts if (post.source or "unknown") == source]
        source_baseline = [post for post in baseline_posts if (post.source or "unknown") == source]
        unique_authors = {
            post.author_id_hash or post.author_handle or f"anon:{post.post_id}"
            for post in source_recent
        }
        recent_stance = _stance_score(source_recent)
        baseline_stance = _stance_score(source_baseline)
        summaries.append(
            SocialSourceSummary(
                source=source,
                total_posts=len(source_posts),
                informative_posts=len(source_informative),
                recent_posts=len(source_recent),
                baseline_posts=len(source_baseline),
                unique_authors=len(unique_authors),
                recent_stance=recent_stance,
                baseline_stance=baseline_stance,
                delta=recent_stance - baseline_stance,
                hard_negative_ratio=_hard_negative_ratio(source_recent),
                top_bullish_themes=_top_themes(source_recent, positive=True),
                top_bearish_themes=_top_themes(source_recent, positive=False),
                representative_posts=_representative_posts(source_recent),
            )
        )
    return summaries


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _resolve_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return timezone.utc
