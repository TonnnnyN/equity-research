from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest import TestCase

from market_sentiment.models import (
    ActionState,
    EventTag,
    Layer,
    OfficialEvent,
    PriceBar,
    Security,
    SocialPost,
    SocialReboundState,
    SourceStatus,
    TriggerResult,
)
from market_sentiment.scoring import build_scorecard, score_social_rebound
from market_sentiment.social_rebound import annotate_posts, build_social_snapshot


def make_post(
    *,
    ticker: str = "MSFT",
    hours_ago: int,
    title: str,
    body: str = "",
    author: str = "author-1",
    source: str = "reddit",
    community: str = "stocks",
    score: int = 10,
    comments: int = 3,
) -> SocialPost:
    now = datetime(2026, 3, 26, 16, 0, 0)
    return SocialPost(
        ticker=ticker,
        source=source,
        community=community,
        post_id=f"{source}-{community}-{author}-{hours_ago}-{score}",
        created_at=now - timedelta(hours=hours_ago),
        title=title,
        body=body,
        url=f"https://example.com/{source}/{community}/{author}/{hours_ago}",
        author_handle=author,
        author_id_hash=author,
        engagement_score=float(score + comments),
        matched_text=True,
    )


class SocialReboundTests(TestCase):
    def test_build_social_snapshot_detects_mild_rebound_from_recent_posts(self) -> None:
        posts = [
            make_post(hours_ago=12, author="bull-1", title="MSFT demand looks stronger and margins are improving"),
            make_post(hours_ago=20, author="bull-2", title="MSFT backlog and cloud demand look solid"),
            make_post(hours_ago=30, author="bull-3", title="MSFT earnings setup improving after the selloff"),
            make_post(hours_ago=40, author="bull-4", title="MSFT looks undervalued with strong cash flow"),
            make_post(hours_ago=60, author="bull-5", title="MSFT product cycle looks healthy and improving"),
            make_post(hours_ago=120, author="bear-1", title="MSFT demand weak and guidance cut risk remains"),
            make_post(hours_ago=160, author="bear-2", title="MSFT margins weak and growth slowing badly"),
            make_post(hours_ago=200, author="bear-3", title="MSFT valuation still weak after the drop"),
            make_post(hours_ago=220, author="bear-4", title="MSFT quarter could miss and growth looks weak"),
            make_post(hours_ago=260, author="bear-5", title="MSFT concerns remain around weak enterprise spend"),
        ]

        snapshot = build_social_snapshot(
            ticker="MSFT",
            run_date=date(2026, 3, 26),
            posts=posts,
            min_informative_posts=4,
            min_unique_authors=4,
            min_sources=1,
            max_author_share=0.40,
            recent_window_hours=72,
            baseline_days=14,
            provider_names={"reddit"},
        )

        self.assertEqual(snapshot.state, SocialReboundState.STRONG_REBOUND)
        self.assertGreater(snapshot.score, 0)
        self.assertGreater(snapshot.delta, 0)

    def test_positive_social_rebound_only_upgrades_one_step(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        social_snapshot = build_social_snapshot(
            ticker="MSFT",
            run_date=date(2026, 3, 26),
            posts=[
                make_post(hours_ago=8, author="bull-1", title="MSFT demand improving with stronger backlog"),
                make_post(hours_ago=12, author="bull-2", title="MSFT cloud demand improving and margins stabilizing"),
                make_post(hours_ago=16, author="bull-3", title="MSFT rebound looks healthier after the panic"),
                make_post(hours_ago=20, author="bull-4", title="MSFT cash flow story looks stronger now"),
                make_post(hours_ago=120, author="bear-1", title="MSFT was weak before this rebound"),
                make_post(hours_ago=140, author="bear-2", title="MSFT guidance concerns were everywhere last week"),
                make_post(hours_ago=160, author="bear-3", title="MSFT looked weak before sentiment improved"),
                make_post(hours_ago=180, author="bear-4", title="MSFT had weak positioning before the recent turn"),
            ],
            min_informative_posts=4,
            min_unique_authors=4,
            min_sources=1,
            max_author_share=0.40,
            recent_window_hours=72,
            baseline_days=14,
            provider_names={"reddit"},
        )
        trigger = TriggerResult(
            triggered=True,
            reasons=["ten_day_drawdown"],
            ten_day_drawdown=0.18,
            twenty_day_drawdown=0.22,
            relative_underperformance=0.08,
            new_low=False,
        )
        # Build realistic 25-bar price series: ~22% decline over first ~18 bars, then modest bounce
        base_date = date(2026, 2, 28)
        prices = []
        start_price = 125.0
        trough_price = 97.5  # ~22% down from 125
        end_price = 102.0    # bounced back to ~81.6% recovery

        for i in range(25):
            current_date = base_date + timedelta(days=i)
            # Smooth decline phase (bars 0-17): from 125 to 97.5
            if i < 18:
                progress = i / 17.0  # 0 to 1
                close_price = start_price - (start_price - trough_price) * progress
                # Add minor noise; low is slightly below close, high is above
                low = close_price - 1.5
                high = close_price + 0.8
                open_price = close_price + 0.5
            # Recovery phase (bars 18-24): from 97.5 back to 102
            else:
                progress = (i - 18) / 6.0  # 0 to 1 over 6 bars
                close_price = trough_price + (end_price - trough_price) * progress
                # Strong closes in recovery: near the daily high
                low = close_price - 0.5
                high = close_price + 1.2
                open_price = close_price - 0.7

            prices.append(
                PriceBar(
                    ticker="MSFT",
                    trading_date=current_date,
                    open=round(open_price, 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    close=round(close_price, 2),
                    volume=1000000.0,
                    source="stooq",
                )
            )

        context = type(
            "Context",
            (),
            {
                "security": security,
                "benchmark_ticker": "QQQ",
                "prices": prices,  # ensure price data ok for P0-3
                "benchmark_prices": [],
                "official_events": [
                    OfficialEvent(
                        ticker="MSFT",
                        event_time=datetime(2026, 3, 20, 16, 30),
                        form_type="8-K",
                        title="MSFT announces strategic update",
                        url="https://example.com",
                        source="sec",
                    )
                ],  # tightened in P1-3
                "fundamentals": None,
                "macro": [],
                "source_statuses": [
                    SourceStatus(source="sec", success=True),
                    SourceStatus(source="stooq", success=True),
                ],
                "social_snapshot": social_snapshot,
                "social_posts_sample": [],
                "social_source_statuses": [SourceStatus(source="reddit", success=True)],
            },
        )()

        scorecard = build_scorecard(
            run_date=date(2026, 3, 26),
            context=context,
            trigger=trigger,
            event_tag=EventTag.COMPANY_SPECIFIC,
            peer_contexts=[],
        )

        self.assertEqual(scorecard.state, ActionState.STARTER)  # tightened in P1-3
        self.assertGreater(scorecard.social_rebound.score, 0)

    def test_annotate_posts_adds_theme_bonus_for_cash_flow_language(self) -> None:
        posts = annotate_posts(
            [
                make_post(
                    hours_ago=6,
                    author="bull-1",
                    title="MSFT cash flow continues improving with stronger backlog and healthier demand",
                    body="Operating cash flow is improving materially after the selloff and the setup now looks much healthier.",
                    score=8,
                    comments=2,
                )
            ]
        )

        self.assertEqual(posts[0].themes, ["demand", "cash_flow"])
        self.assertGreaterEqual(posts[0].quality_score or 0.0, 0.8)

    def test_build_social_snapshot_uses_trusted_provider_count_for_breadth(self) -> None:
        snapshot = build_social_snapshot(
            ticker="MSFT",
            run_date=date(2026, 3, 26),
            posts=[
                make_post(hours_ago=8, author="bull-1", title="MSFT demand improving with stronger backlog", source="reddit"),
                make_post(hours_ago=12, author="bull-2", title="MSFT cloud demand improving and margins stabilizing", source="x"),
            ],
            min_informative_posts=2,
            min_unique_authors=2,
            min_sources=2,
            max_author_share=0.60,
            recent_window_hours=72,
            baseline_days=14,
            provider_names={"reddit"},
        )

        self.assertEqual(snapshot.provider_count, 1)
        self.assertIn("social_source_count_insufficient", snapshot.notes)

    def test_build_social_snapshot_uses_project_timezone_for_run_cutoff(self) -> None:
        snapshot = build_social_snapshot(
            ticker="MSFT",
            run_date=date(2026, 3, 26),
            posts=[
                SocialPost(
                    ticker="MSFT",
                    source="reddit",
                    community="stocks",
                    post_id="late-local-post",
                    created_at=datetime.fromisoformat("2026-03-27T03:30:00+00:00"),
                    title="Microsoft demand improving late in the New York session",
                    body="Backlog and cash flow both look stronger.",
                    url="https://example.com/post",
                    author_handle="late-bull",
                    author_id_hash="late-bull",
                    engagement_score=14.0,
                    matched_text=True,
                )
            ],
            min_informative_posts=1,
            min_unique_authors=1,
            min_sources=1,
            max_author_share=1.0,
            recent_window_hours=72,
            baseline_days=14,
            provider_names={"reddit"},
            timezone_name="America/New_York",
        )

        self.assertEqual(snapshot.recent_posts, 1)

    def test_partial_coverage_halves_negative_social_score(self) -> None:
        snapshot = build_social_snapshot(
            ticker="MSFT",
            run_date=date(2026, 3, 26),
            posts=[
                make_post(hours_ago=8, author="bear-1", title="MSFT demand weakening significantly"),
                make_post(hours_ago=12, author="bear-2", title="MSFT cloud adoption slowing"),
                make_post(hours_ago=16, author="bear-3", title="MSFT margins compressing badly"),
                make_post(hours_ago=20, author="bear-4", title="MSFT guidance cuts likely"),
            ],
            min_informative_posts=4,
            min_unique_authors=4,
            min_sources=1,
            max_author_share=0.40,
            recent_window_hours=72,
            baseline_days=14,
            provider_names={"reddit"},
        )

        # Ensure snapshot has negative score
        self.assertLess(snapshot.score, 0)
        original_score = snapshot.score

        # Score with partial_coverage=True
        result = score_social_rebound(snapshot, [], partial_coverage=True)

        # Expected: score should be halved (rounded toward zero)
        expected_score = -(abs(original_score) // 2)
        self.assertEqual(result.score, expected_score)
        self.assertIn("social_negative_halved_under_partial_coverage", result.notes)
