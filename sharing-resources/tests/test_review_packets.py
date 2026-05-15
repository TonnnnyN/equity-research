from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from market_sentiment.models import (
    ActionState,
    BucketScore,
    DailyRunReport,
    EventTag,
    FundamentalSnapshot,
    Layer,
    MacroObservation,
    OfficialEvent,
    OptionContract,
    OptionSnapshot,
    PipelineContext,
    PriceBar,
    PriceWindow,
    ScoreCard,
    Security,
    SocialPost,
    SocialReboundState,
    SocialSnapshot,
    SocialSourceSummary,
    SocialThemeSummary,
    SourceStatus,
    TriggerResult,
)
from market_sentiment.manual_agent_report import render_manual_agent_report
from market_sentiment.review_packets import build_review_packet
from market_sentiment.storage import Storage


class ReviewPacketTests(TestCase):
    def test_build_review_packet_contains_rule_and_evidence_sections(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[
                PriceBar("MSFT", date(2026, 3, 25), 100, 101, 99, 100, 1000, "alpha"),
                PriceBar("MSFT", date(2026, 3, 26), 95, 96, 94, 95, 1000, "alpha"),
            ],
            benchmark_prices=[
                PriceBar("QQQ", date(2026, 3, 25), 100, 101, 99, 100, 1000, "alpha"),
                PriceBar("QQQ", date(2026, 3, 26), 99, 100, 98, 99, 1000, "alpha"),
            ],
            official_events=[
                OfficialEvent(
                    ticker="MSFT",
                    event_time=datetime(2026, 3, 20),
                    form_type="10-Q",
                    title="MSFT filed quarterly results",
                    url="https://example.com",
                    source="sec",
                )
            ],
            fundamentals=FundamentalSnapshot(
                ticker="MSFT",
                cik="1",
                period_end=date(2025, 12, 31),
                filed_on=date(2026, 2, 1),
                revenue_latest=120,
                revenue_previous=100,
                operating_cashflow_latest=50,
                operating_cashflow_previous=40,
                capex_latest=10,
                cash_latest=90,
                debt_latest=40,
                source="sec_companyfacts",
            ),
            macro=[MacroObservation(name="dgs10", observed_on=date(2026, 3, 26), value=4.2, source="fred")],
            source_statuses=[SourceStatus(source="alpha", success=True, message="ok")],
        )
        scorecard = ScoreCard(
            run_date=date(2026, 3, 26),
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(
                triggered=True,
                reasons=["ten_day_drawdown", "fresh_low"],
                ten_day_drawdown=0.1,
                twenty_day_drawdown=0.12,
                relative_underperformance=0.06,
                new_low=True,
                ten_day_window=PriceWindow(
                    start_date=date(2026, 3, 12),
                    end_date=date(2026, 3, 26),
                    start_close=105.0,
                    end_close=95.0,
                    drawdown=0.0952,
                ),
                twenty_day_window=PriceWindow(
                    start_date=date(2026, 2, 27),
                    end_date=date(2026, 3, 26),
                    start_close=108.0,
                    end_close=95.0,
                    drawdown=0.1204,
                ),
                benchmark_twenty_day_window=PriceWindow(
                    start_date=date(2026, 2, 27),
                    end_date=date(2026, 3, 26),
                    start_close=102.0,
                    end_close=99.0,
                    drawdown=0.0294,
                ),
                fresh_low_window=3,
            ),
            fundamentals=BucketScore("fundamentals", 30, 30, []),
            sentiment=BucketScore("sentiment", 10, 15, []),
            chain_confirmation=BucketScore("chain_confirmation", 20, 20, []),
            price_flow=BucketScore("price_flow", 5, 15, []),
            risk_red_flags=BucketScore("risk_red_flags", 17, 20, []),
            total_score=82,
            state=ActionState.WATCH,
            partial_coverage=False,
            evidence=["10-Q", "ten_day_drawdown", "fresh_low"],
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        self.assertEqual(packet["security"]["ticker"], "MSFT")
        self.assertEqual(packet["schema_version"], "1.2")
        self.assertEqual(packet["rule_engine_precheck"]["state"], "Watch")
        self.assertEqual(packet["trigger_summary"]["reasons"], ["ten_day_drawdown", "fresh_low"])
        self.assertEqual(packet["trigger_summary"]["ten_day_window"]["start_date"], "2026-03-12")
        self.assertIn("fundamentals_snapshot", packet)
        self.assertIn("social_summary", packet)
        self.assertIn("decision_summary", packet)
        self.assertIn("agent_questions", packet)

    def test_build_review_packet_includes_option_summary(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            options_snapshot=OptionSnapshot(
                ticker="MSFT",
                run_date=date(2026, 3, 26),
                source="alpha_vantage_options",
                contract_count=2,
                call_contracts=1,
                put_contracts=1,
                total_call_volume=220,
                total_put_volume=120,
                total_call_open_interest=1400,
                total_put_open_interest=900,
                put_call_volume_ratio=120 / 220,
                put_call_open_interest_ratio=900 / 1400,
                implied_volatility_avg=0.325,
                nearest_expiration=date(2026, 4, 17),
                nearest_days_to_expiry=22,
                max_call_open_interest_strike=400.0,
                max_put_open_interest_strike=380.0,
                top_contracts=[
                    OptionContract(
                        contract_id="MSFT260417C00400000",
                        expiration=date(2026, 4, 17),
                        strike=400.0,
                        option_type="call",
                        volume=220,
                        open_interest=1400,
                        implied_volatility=0.31,
                        last_price=12.40,
                    )
                ],
            ),
            options_source_statuses=[SourceStatus(source="alpha_vantage_options", success=True, message="ok")],
        )
        scorecard = ScoreCard(
            run_date=date(2026, 3, 26),
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 13, 20, ["options_call_skew_constructive"]),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=73,
            state=ActionState.STARTER,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        self.assertIn("option_summary", packet)
        self.assertEqual(packet["option_summary"]["contract_count"], 2)
        self.assertEqual(packet["option_summary"]["top_contracts"][0]["contract_id"], "MSFT260417C00400000")

    def test_social_summary_exposes_concise_source_brief(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
            social_snapshot=SocialSnapshot(
                ticker="MSFT",
                run_date=date(2026, 3, 26),
                recent_window_hours=72,
                baseline_days=14,
                provider_count=2,
                community_count=2,
                total_posts=12,
                informative_posts=10,
                recent_posts=6,
                baseline_posts=4,
                unique_authors=5,
                author_concentration=0.4,
                recent_stance=0.35,
                baseline_stance=0.05,
                delta=0.30,
                breadth=0.8,
                hard_negative_ratio=0.1,
                state=SocialReboundState.MILD_REBOUND,
                score=4,
                provider_post_counts={"reddit": 8, "x": 4},
                recent_provider_post_counts={"reddit": 4, "x": 2},
                source_summaries=[
                    SocialSourceSummary(
                        source="reddit",
                        total_posts=8,
                        informative_posts=7,
                        recent_posts=4,
                        baseline_posts=3,
                        unique_authors=3,
                        recent_stance=0.4,
                        baseline_stance=0.1,
                        delta=0.3,
                        hard_negative_ratio=0.0,
                        top_bullish_themes=[SocialThemeSummary(label="demand", direction="positive", count=2)],
                        top_bearish_themes=[],
                        representative_posts=[
                            SocialPost(
                                ticker="MSFT",
                                source="reddit",
                                community="stocks",
                                post_id="r1",
                                created_at=datetime(2026, 3, 26, 10, 0, 0),
                                title="MSFT demand improving",
                                body="",
                                url="https://example.com/r1",
                            )
                        ],
                    ),
                    SocialSourceSummary(
                        source="x",
                        total_posts=4,
                        informative_posts=3,
                        recent_posts=2,
                        baseline_posts=1,
                        unique_authors=2,
                        recent_stance=-0.15,
                        baseline_stance=-0.05,
                        delta=-0.10,
                        hard_negative_ratio=0.5,
                        top_bullish_themes=[],
                        top_bearish_themes=[SocialThemeSummary(label="valuation", direction="negative", count=1)],
                        representative_posts=[
                            SocialPost(
                                ticker="MSFT",
                                source="x",
                                community="x",
                                post_id="x1",
                                created_at=datetime(2026, 3, 26, 11, 0, 0),
                                title="MSFT valuation debate",
                                body="",
                                url="https://example.com/x1",
                            )
                        ],
                    ),
                ],
            ),
        )
        scorecard = ScoreCard(
            run_date=date(2026, 3, 26),
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        source_brief = packet["social_summary"]["source_brief"]
        self.assertEqual(source_brief[0]["source"], "reddit")
        self.assertEqual(source_brief[0]["overall_tone"], "positive_improving")
        self.assertEqual(source_brief[1]["source"], "x")
        self.assertEqual(source_brief[1]["top_bearish_theme"], "valuation")

    def test_render_manual_agent_report_includes_action_standards_and_numeric_evidence(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        scorecard = ScoreCard(
            run_date=date(2026, 3, 26),
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(
                triggered=True,
                reasons=["ten_day_drawdown", "fresh_low"],
                ten_day_drawdown=0.1,
                twenty_day_drawdown=0.12,
                relative_underperformance=0.06,
                new_low=True,
                ten_day_window=PriceWindow(
                    start_date=date(2026, 3, 12),
                    end_date=date(2026, 3, 26),
                    start_close=105.0,
                    end_close=95.0,
                    drawdown=0.0952,
                ),
                twenty_day_window=PriceWindow(
                    start_date=date(2026, 2, 27),
                    end_date=date(2026, 3, 26),
                    start_close=108.0,
                    end_close=95.0,
                    drawdown=0.1204,
                ),
                benchmark_twenty_day_window=PriceWindow(
                    start_date=date(2026, 2, 27),
                    end_date=date(2026, 3, 26),
                    start_close=102.0,
                    end_close=99.0,
                    drawdown=0.0294,
                ),
                fresh_low_window=3,
            ),
            fundamentals=BucketScore("fundamentals", 30, 30, ["positive_revenue_growth"]),
            sentiment=BucketScore("sentiment", 10, 15, ["current_reporting_active"]),
            chain_confirmation=BucketScore("chain_confirmation", 20, 20, ["isolated_dip"]),
            price_flow=BucketScore("price_flow", 5, 15, ["ten_day_drawdown"]),
            risk_red_flags=BucketScore("risk_red_flags", 17, 20, ["still_making_lows"]),
            total_score=82,
            state=ActionState.WATCH,
            partial_coverage=False,
            evidence=["10-Q", "ten_day_drawdown", "fresh_low"],
        )
        packet = {
            "benchmark_ticker": "QQQ",
            "trigger_summary": {
                "ten_day_window": {
                    "start_date": "2026-03-12",
                    "end_date": "2026-03-26",
                    "start_close": 105.0,
                    "end_close": 95.0,
                    "drawdown": 0.0952,
                },
                "twenty_day_window": {
                    "start_date": "2026-02-27",
                    "end_date": "2026-03-26",
                    "start_close": 108.0,
                    "end_close": 95.0,
                    "drawdown": 0.1204,
                },
                "benchmark_twenty_day_window": {
                    "start_date": "2026-02-27",
                    "end_date": "2026-03-26",
                    "start_close": 102.0,
                    "end_close": 99.0,
                    "drawdown": 0.0294,
                },
                "fresh_low_window": 3,
            },
            "fundamentals_snapshot": {
                "period_end": "2025-12-31",
                "filed_on": "2026-02-01",
                "revenue_latest": 120.0,
                "revenue_previous": 100.0,
                "operating_cashflow_latest": 50.0,
                "operating_cashflow_previous": 40.0,
                "cash_latest": 90.0,
                "debt_latest": 40.0,
                "capex_latest": 10.0,
                "derived_metrics": {"revenue_growth": 0.2, "cash_minus_debt": 50.0},
            },
            "official_events": [{"event_time": "2026-03-20T00:00:00", "form_type": "10-Q", "title": "MSFT filed quarterly results"}],
            "bucket_scores": {
                "fundamentals": {"score": 30, "max_score": 30, "notes": ["positive_revenue_growth"]},
                "sentiment": {"score": 10, "max_score": 15, "notes": ["current_reporting_active"]},
                "chain_confirmation": {"score": 20, "max_score": 20, "notes": ["isolated_dip"]},
                "price_flow": {"score": 5, "max_score": 15, "notes": ["ten_day_drawdown"]},
                "risk_red_flags": {"score": 17, "max_score": 20, "notes": ["still_making_lows"]},
            },
            "decision_summary": {
                "top_positive_signals": ["营收同比增长 20.0%", "经营现金流为正"],
                "top_risk_signals": ["价格仍在近期新低区间"],
                "next_checks": ["先观察是否脱离近期新低区间"],
            },
            "macro_summary": {"dgs10": {"observed_on": "2026-03-26", "value": 4.2}},
            "source_health": [],
        }

        report = render_manual_agent_report(
            DailyRunReport(
                run_date=date(2026, 3, 26),
                generated_at=datetime(2026, 3, 26, 14, 0, 0),
                triggered_count=1,
                scorecards=[scorecard],
                source_statuses=[],
            ),
            {"MSFT": packet},
        )

        self.assertIn("动作标签说明", report)
        self.assertIn("2026-03-12", report)
        self.assertIn("105.00", report)
        self.assertIn("营收", report)
        self.assertIn("动作解释", report)

    def test_manual_agent_report_renders_source_level_social_summary(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        scorecard = ScoreCard(
            run_date=date(2026, 3, 26),
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )
        packet = {
            "benchmark_ticker": "QQQ",
            "trigger_summary": {},
            "fundamentals_snapshot": None,
            "official_events": [],
            "bucket_scores": {},
            "decision_summary": {"top_positive_signals": [], "top_risk_signals": [], "next_checks": []},
            "macro_summary": {},
            "source_health": [],
            "social_summary": {
                "state": "mild_rebound",
                "score": 4,
                "recent_window_hours": 72,
                "recent_posts": 6,
                "baseline_posts": 4,
                "unique_authors": 5,
                "delta": 0.3,
                "breadth": 0.8,
                "hard_negative_ratio": 0.1,
                "provider_post_counts": {"reddit": 8, "x": 4},
                "recent_provider_post_counts": {"reddit": 4, "x": 2},
                "top_bullish_themes": [],
                "top_bearish_themes": [],
                "source_brief": [
                    {
                        "source": "reddit",
                        "total_posts": 8,
                        "recent_posts": 4,
                        "unique_authors": 3,
                        "overall_tone": "positive_improving",
                        "delta": 0.3,
                        "hard_negative_ratio": 0.0,
                        "top_bullish_theme": "demand",
                        "representative_title": "MSFT demand improving",
                    },
                    {
                        "source": "x",
                        "total_posts": 4,
                        "recent_posts": 2,
                        "unique_authors": 2,
                        "overall_tone": "negative",
                        "delta": -0.1,
                        "hard_negative_ratio": 0.5,
                        "top_bearish_theme": "valuation",
                        "representative_title": "MSFT valuation debate",
                    },
                ],
            },
        }

        report = render_manual_agent_report(
            DailyRunReport(
                run_date=date(2026, 3, 26),
                generated_at=datetime(2026, 3, 26, 14, 0, 0),
                triggered_count=1,
                scorecards=[scorecard],
                source_statuses=[],
            ),
            {"MSFT": packet},
        )

        self.assertIn("分来源摘要", report)
        self.assertIn("reddit", report)
        self.assertIn("positive_improving", report)
        self.assertIn("MSFT valuation debate", report)

    def test_manual_agent_report_renders_option_summary(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        scorecard = ScoreCard(
            run_date=date(2026, 3, 26),
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 13, 20, ["options_call_skew_constructive"]),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=73,
            state=ActionState.STARTER,
        )
        packet = {
            "benchmark_ticker": "QQQ",
            "trigger_summary": {},
            "fundamentals_snapshot": None,
            "official_events": [],
            "bucket_scores": {
                "chain_confirmation": {
                    "score": 13,
                    "max_score": 20,
                    "notes": ["options_call_skew_constructive"],
                }
            },
            "decision_summary": {"top_positive_signals": [], "top_risk_signals": [], "next_checks": []},
            "macro_summary": {},
            "source_health": [],
            "option_summary": {
                "contract_count": 2,
                "call_contracts": 1,
                "put_contracts": 1,
                "put_call_volume_ratio": 0.5455,
                "put_call_open_interest_ratio": 0.6429,
                "implied_volatility_avg": 0.325,
                "nearest_expiration": "2026-04-17",
                "nearest_days_to_expiry": 22,
                "max_call_open_interest_strike": 400.0,
                "max_put_open_interest_strike": 380.0,
                "top_contracts": [
                    {
                        "contract_id": "MSFT260417C00400000",
                        "option_type": "call",
                        "strike": 400.0,
                        "volume": 220,
                        "open_interest": 1400,
                        "last_price": 12.4,
                    }
                ],
            },
        }

        report = render_manual_agent_report(
            DailyRunReport(
                run_date=date(2026, 3, 26),
                generated_at=datetime(2026, 3, 26, 14, 0, 0),
                triggered_count=1,
                scorecards=[scorecard],
                source_statuses=[],
            ),
            {"MSFT": packet},
        )

        self.assertIn("期权链参考", report)
        self.assertIn("MSFT260417C00400000", report)
        self.assertIn("0.5455", report)

    def test_freshness_block_present_with_fresh_data(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 3, 26)
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[
                OfficialEvent(
                    ticker="MSFT",
                    event_time=datetime(2026, 3, 20),
                    form_type="10-Q",
                    title="MSFT filed quarterly results",
                    url="https://example.com",
                    source="sec",
                )
            ],
            fundamentals=FundamentalSnapshot(
                ticker="MSFT",
                cik="1",
                period_end=date(2026, 3, 10),  # 16 days before run_date
                filed_on=date(2026, 3, 15),
                revenue_latest=120,
                revenue_previous=100,
                operating_cashflow_latest=50,
                operating_cashflow_previous=40,
                capex_latest=10,
                cash_latest=90,
                debt_latest=40,
                source="sec_companyfacts",
            ),
            macro=[],
            source_statuses=[],
        )
        scorecard = ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        self.assertIn("freshness", packet)
        freshness = packet["freshness"]
        self.assertEqual(freshness["run_date"], "2026-03-26")
        self.assertEqual(freshness["fundamentals_period_end"], "2026-03-10")
        self.assertEqual(freshness["fundamentals_age_days"], 16)
        self.assertEqual(freshness["latest_official_event_date"], "2026-03-20")
        self.assertEqual(freshness["official_event_age_days"], 6)
        self.assertEqual(freshness["caveats"], [])

    def test_freshness_block_warns_on_stale_fundamentals(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 3, 26)
        stale_period_end = date(2025, 12, 15)  # 101 days before run_date
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=FundamentalSnapshot(
                ticker="MSFT",
                cik="1",
                period_end=stale_period_end,
                filed_on=date(2025, 12, 31),
                revenue_latest=120,
                revenue_previous=100,
                operating_cashflow_latest=50,
                operating_cashflow_previous=40,
                capex_latest=10,
                cash_latest=90,
                debt_latest=40,
                source="sec_companyfacts",
            ),
            macro=[],
            source_statuses=[],
        )
        scorecard = ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        freshness = packet["freshness"]
        self.assertEqual(freshness["fundamentals_age_days"], 101)
        self.assertTrue(any("current quarter likely unreported" in caveat for caveat in freshness["caveats"]))

    def test_freshness_block_handles_missing_fundamentals(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 3, 26)
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=None,
            macro=[],
            source_statuses=[],
        )
        scorecard = ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        freshness = packet["freshness"]
        self.assertIsNone(freshness["fundamentals_period_end"])
        self.assertIsNone(freshness["fundamentals_age_days"])
        self.assertTrue(any("no fundamentals snapshot available" in caveat for caveat in freshness["caveats"]))

    def test_freshness_block_handles_no_events(self) -> None:
        security = Security(ticker="MSFT", name="Microsoft", layer=Layer.AI_APPLICATIONS, benchmark="QQQ")
        run_date = date(2026, 3, 26)
        context = PipelineContext(
            security=security,
            benchmark_ticker="QQQ",
            prices=[],
            benchmark_prices=[],
            official_events=[],
            fundamentals=FundamentalSnapshot(
                ticker="MSFT",
                cik="1",
                period_end=date(2026, 3, 10),
                filed_on=date(2026, 3, 15),
                revenue_latest=120,
                revenue_previous=100,
                operating_cashflow_latest=50,
                operating_cashflow_previous=40,
                capex_latest=10,
                cash_latest=90,
                debt_latest=40,
                source="sec_companyfacts",
            ),
            macro=[],
            source_statuses=[],
        )
        scorecard = ScoreCard(
            run_date=run_date,
            security=security,
            event_tag=EventTag.COMPANY_SPECIFIC,
            triggered=True,
            trigger=TriggerResult(triggered=True, reasons=[]),
            fundamentals=BucketScore("fundamentals", 10, 30),
            sentiment=BucketScore("sentiment", 5, 15),
            chain_confirmation=BucketScore("chain_confirmation", 10, 20),
            price_flow=BucketScore("price_flow", 3, 15),
            risk_red_flags=BucketScore("risk_red_flags", 12, 20),
            total_score=70,
            state=ActionState.WATCH,
        )

        packet = build_review_packet(datetime(2026, 3, 26, 14, 0, 0), context, scorecard)

        freshness = packet["freshness"]
        self.assertIsNone(freshness["latest_official_event_date"])
        self.assertIsNone(freshness["official_event_age_days"])
        self.assertTrue(any("no official events available" in caveat for caveat in freshness["caveats"]))

