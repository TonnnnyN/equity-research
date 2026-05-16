from __future__ import annotations

from collections import Counter
from typing import Any

from market_sentiment.models import ActionState, DailyRunReport, ScoreCard


ACTION_PRIORITY = {
    ActionState.ADD: 0,
    ActionState.STARTER: 1,
    ActionState.WATCH: 2,
    ActionState.REJECT: 3,
    ActionState.EXIT: 4,
}

ACTION_DESCRIPTIONS = {
    ActionState.REJECT: "不满足当前研究动作门槛。要么出现硬 veto，要么总分低于 65，当前不建议采取新动作。",
    ActionState.WATCH: "进入重点观察名单。通常表示证据有价值，但尚未确认企稳，或仍处在 `fresh_low` 约束下。",
    ActionState.STARTER: "允许小仓位试错。总分通常在 72-79，且价格没有被 `fresh_low` 压制。",
    ActionState.ADD: "允许在已有研究结论基础上继续加大仓位。总分通常 80 以上，且没有 `fresh_low` 上限约束。",
    ActionState.EXIT: "预留给持仓管理动作，本项目当前日终筛选默认不会自动产出这个状态。",
}

EVENT_TAG_LABELS = {
    "company_specific": "公司特异性",
    "sector_wide": "板块共振",
    "market_wide": "市场普跌",
    "unknown": "未知",
}

BUCKET_LABELS = {
    "fundamentals": "基本面",
    "sentiment": "披露情绪",
    "social_rebound": "社交反弹",
    "chain_confirmation": "链条确认",
    "price_flow": "价格流",
    "risk_red_flags": "风险红旗",
}

SOURCE_LABELS = {
    "reddit": "Reddit",
    "x": "X",
    "forum": "Forum",
}

NOTE_LABELS = {
    "companyfacts_snapshot_present": "已拿到 companyfacts 快照",
    "positive_revenue_growth": "营收同比为正",
    "negative_revenue_growth": "营收同比为负",
    "positive_operating_cashflow": "经营现金流为正",
    "negative_operating_cashflow": "经营现金流为负",
    "cash_exceeds_debt": "现金覆盖全部债务",
    "cash_covers_half_debt": "现金覆盖超过半数债务",
    "capex_disclosed": "披露了资本开支",
    "fresh_companyfacts_period": "财务快照较新",
    "stale_companyfacts_period": "财务快照偏旧",
    "fresh_official_updates": "最近 15 天内有官方更新",
    "moderately_fresh_updates": "最近 45 天内有官方更新",
    "stale_updates": "官方更新偏旧",
    "core_financial_filing_present": "近期有 10-Q / 10-K",
    "recent_current_report": "近期有 8-K",
    "no_official_events_or_companyfacts": "缺少官方事件与 companyfacts",
    "missing_event_text": "缺少事件文本",
    "fresh_disclosure_window": "披露窗口较新",
    "recent_disclosure_window": "披露窗口较近",
    "current_reporting_active": "近期持续披露",
    "late_or_withdrawn_form_detected": "出现延迟/撤回类表单",
    "financial_update_detected": "标题中出现财报/业绩类关键词",
    "negative_keyword_detected": "标题中出现负面风险词",
    "peer_coverage_present": "有同层 peers 可比较",
    "isolated_dip": "更像公司特异性回撤",
    "sector_pressure": "更像板块压力",
    "market_pressure": "更像市场压力",
    "macro_context_present": "宏观上下文已获取",
    "ten_day_drawdown": "10 日跌幅达到条件",
    "twenty_day_drawdown": "20 日跌幅达到条件",
    "relative_underperformance": "相对基准跑输达到条件",
    "not_at_fresh_low": "当前不在近期新低",
    "hard_veto_negative_keyword": "官方披露触发硬 veto 风险词",
    "elevated_form_risk": "存在延迟/撤回类表单风险",
    "companyfacts_revenue_break": "营收出现明显断裂",
    "companyfacts_negative_operating_cashflow": "经营现金流为负",
    "hard_veto_structural_break": "基本面出现结构性断裂",
    "still_making_lows": "价格仍在创近期新低",
    "no_official_coverage": "缺少官方披露覆盖",
    "social_unavailable": "社交层暂无可用数据",
    "social_recent_sample_insufficient": "最近窗口有效帖子不足",
    "social_author_sample_insufficient": "独立作者样本不足",
    "social_source_count_insufficient": "社交来源数量不足",
    "social_author_concentration_high": "作者集中度过高",
    "social_hard_negative_topics_dominant": "社交讨论中硬负面主题占主导",
    "social_source_fetch_failed": "部分社交源抓取失败",
    "social_strong_rebound": "社交情绪明显从坏转好",
    "social_mild_rebound": "社交情绪温和改善",
    "social_flat_unclear": "社交情绪方向不清晰",
    "social_worsening": "社交情绪继续恶化",
    "social_insufficient_data": "社交情绪样本不足",
    "social_positive_blocked_by_partial_core_coverage": "核心数据未完整覆盖，社交正分被阻断",
    "options_unavailable": "期权链数据暂不可用",
    "options_activity_present": "已拿到期权链活动摘要",
    "options_call_skew_constructive": "期权成交更偏向 call 侧",
    "options_put_skew_defensive": "期权成交更偏向 put 侧",
    "options_open_interest_constructive": "期权未平仓更偏向 call 侧",
    "options_open_interest_defensive": "期权未平仓更偏向 put 侧",
    "options_near_expiry_window": "期权处于临近到期窗口",
    "options_contract_cap_applied": "期权合约样本触发上限截断",
}


def render_manual_agent_report(
    report: DailyRunReport,
    packets: dict[str, dict[str, Any]],
    decision_tracking_result: Any = None,
) -> str:
    lines = [
        f"# {report.run_date.isoformat()} 手动 Agent 详细复核底稿",
        "",
        "这份报告是 `Python 标准化 + 规则引擎预判` 之后的中文详细底稿。",
        "定位是帮助手动 Agent 或人工更快复核，所以会把价格触发、基本面证据、pipeline 信号和动作标准都放到同一份文件里。",
        "以下每个 ticker 给出的 `规则引擎动作` 仅为**机器视角的快速预判**，并非最终结论。复核 Agent 必须自行读取证据卡片中的原始数据（价格序列、SEC 披露、财报快照、社交快照、宏观）独立打分，与规则引擎不一致时在最终报告中明确说明覆盖理由。`hard veto` 仍然生效（破产/欺诈/营收结构性断裂等客观条件下强制 Reject）。",
        "",
        "## 运行摘要",
        f"- 生成时间：`{report.generated_at.isoformat()}`",
        f"- 触发标的数：`{report.triggered_count}`",
        f"- 数据覆盖完整的触发标的：`{sum(1 for card in report.scorecards if not card.partial_coverage)}` / `{len(report.scorecards)}`",
        "- 本文件是本日运行的**唯一**人工/Agent 复核报告,所有触发股的完整证据均在下方;另在 `review_packets/<TICKER>.json` 提供机读结构。",
        "",
        "## 动作标签说明",
    ]
    for state in [ActionState.REJECT, ActionState.WATCH, ActionState.STARTER, ActionState.ADD, ActionState.EXIT]:
        lines.append(f"- `{state.value}`：{ACTION_DESCRIPTIONS[state]}")

    if not report.scorecards:
        lines.extend(["", "## 结果", "- 本次没有触发任何标的。"])
        return "\n".join(lines) + "\n"

    counts = Counter(card.state.value for card in report.scorecards)
    lines.extend(
        [
            "",
            "## 动作分布",
            *(f"- `{state}`：`{counts.get(state, 0)}`" for state in ["Add", "Starter", "Watch", "Reject"]),
            "",
            "## 持仓条件监控",
        ]
    )
    lines.extend(_render_decision_tracking(decision_tracking_result))
    lines.extend(
        [
            "",
            "## 总览表",
            "",
            "| Ticker | 规则动作 | 分数 | 归因 | 10D 跌幅 | 20D 跌幅 | 相对基准 | 新低 | 基本面摘要 |",
            "|---|---|---:|---|---|---|---|---|---|",
        ]
    )

    sorted_cards = sorted(
        report.scorecards,
        key=lambda card: (ACTION_PRIORITY.get(card.state, 99), -card.total_score, card.security.ticker),
    )
    for card in sorted_cards:
        packet = packets.get(card.security.ticker, {})
        lines.append(
            "| "
            + " | ".join(
                [
                    card.security.ticker,
                    card.state.value,
                    str(card.total_score),
                    EVENT_TAG_LABELS.get(card.event_tag.value, card.event_tag.value),
                    _window_compact(packet.get("trigger_summary", {}).get("ten_day_window")),
                    _window_compact(packet.get("trigger_summary", {}).get("twenty_day_window")),
                    _format_pct(card.trigger.relative_underperformance),
                    "是" if card.trigger.new_low else "否",
                    _fundamental_summary(packet.get("fundamentals_snapshot")),
                ]
            )
            + " |"
        )

    lines.extend(["", "## 市场与板块背景"])
    lines.extend(_render_market_context(sorted_cards, packets))

    lines.extend(["", "## 个股证据卡片"])
    for card in sorted_cards:
        packet = packets.get(card.security.ticker, {})
        lines.extend(_render_security_card(card, packet))

    lines.extend(["", "## 证据附录", "", "### Source Health"])
    source_lines = _render_source_health(sorted_cards, packets)
    lines.extend(source_lines if source_lines else ["- 本次触发标的未发现额外的 source health 异常。"])
    return "\n".join(lines) + "\n"


def _render_market_context(scorecards: list[ScoreCard], packets: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    benchmark_rows: dict[str, str] = {}
    for card in scorecards:
        packet = packets.get(card.security.ticker, {})
        benchmark_ticker = packet.get("benchmark_ticker") or card.security.benchmark
        benchmark_window = packet.get("trigger_summary", {}).get("benchmark_twenty_day_window")
        if benchmark_ticker and benchmark_ticker not in benchmark_rows:
            benchmark_rows[benchmark_ticker] = _window_sentence(benchmark_window, benchmark_ticker, "20 日基准")
    for ticker, sentence in sorted(benchmark_rows.items()):
        lines.append(f"- `{ticker}`：{sentence}")

    first_packet = next(iter(packets.values()), {})
    macro_summary = first_packet.get("macro_summary", {})
    if macro_summary:
        lines.append("- 宏观观察：")
        for name, observation in sorted(macro_summary.items()):
            lines.append(
                f"  - `{name}`：`{observation.get('observed_on', 'n/a')}` = `{observation.get('value', 'n/a')}`"
            )
    return lines or ["- 本次没有可展开的市场背景数据。"]


def _render_security_card(card: ScoreCard, packet: dict[str, Any]) -> list[str]:
    trigger_summary = packet.get("trigger_summary", {})
    fundamentals = packet.get("fundamentals_snapshot")
    decision_summary = packet.get("decision_summary", {})
    official_events = packet.get("official_events", [])
    bucket_scores = packet.get("bucket_scores", {})
    social_summary = packet.get("social_summary")
    option_summary = packet.get("option_summary")

    lines = [
        "",
        f"### {card.security.ticker}",
        f"- 机器预判（供参考，需 LLM 独立覆核）：`{card.state.value}`，机器总分 `{card.total_score}`，层级 `{card.security.layer.value}`",
        f"- 动作解释：{_rule_state_reason(card)}",
        f"- 事件归因：`{EVENT_TAG_LABELS.get(card.event_tag.value, card.event_tag.value)}`",
        f"- 数据完整性：`{'完整' if not card.partial_coverage else '部分缺口'}`",
        "",
        "触发证据：",
        f"- 10 日：{_window_sentence(trigger_summary.get('ten_day_window'), card.security.ticker, '10 日区间')}",
        f"- 20 日：{_window_sentence(trigger_summary.get('twenty_day_window'), card.security.ticker, '20 日区间')}",
        f"- 基准：{_window_sentence(trigger_summary.get('benchmark_twenty_day_window'), packet.get('benchmark_ticker', card.security.benchmark), '20 日基准')}",
        f"- 相对基准跑输：`{_format_pct(card.trigger.relative_underperformance)}`",
        f"- `fresh_low`：`{'是' if card.trigger.new_low else '否'}`；比较窗口：最近 `{trigger_summary.get('fresh_low_window', 'n/a')}` 个交易日",
        "",
        "基本面证据：",
    ]

    lines.extend(_render_fundamentals(fundamentals))
    lines.extend(
        [
            "",
            "最近官方披露：",
            *(
                f"- `{event.get('event_time', 'n/a')[:10]}` `{event.get('form_type', 'n/a')}` `{event.get('title', 'n/a')}`"
                for event in official_events[:3]
            ),
        ]
        if official_events
        else ["", "最近官方披露：", "- 本次 packet 中没有可用的官方披露。"]
    )

    lines.extend(["", "社交情绪："])
    lines.extend(_render_social_summary(social_summary))

    lines.extend(["", "期权链参考："])
    lines.extend(_render_option_summary(option_summary))

    lines.extend(["", "Pipeline 信息小结："])
    for bucket_name, payload in bucket_scores.items():
        notes = ", ".join(_translate_note(note) for note in payload.get("notes", [])) or "n/a"
        lines.append(
            f"- {BUCKET_LABELS.get(bucket_name, bucket_name)}：`{payload.get('score', 'n/a')}/{payload.get('max_score', 'n/a')}`；说明：{notes}"
        )

    positives = decision_summary.get("top_positive_signals", [])
    risks = decision_summary.get("top_risk_signals", [])
    next_checks = decision_summary.get("next_checks", [])
    lines.extend(["", "关键正面信号："])
    lines.extend([f"- {item}" for item in positives] or ["- 暂无。"])
    lines.extend(["", "主要风险："])
    lines.extend([f"- {item}" for item in risks] or ["- 暂无。"])
    lines.extend(["", "后续观察点："])
    lines.extend([f"- {item}" for item in next_checks] or ["- 暂无。"])
    return lines


def _render_fundamentals(snapshot: dict[str, Any] | None) -> list[str]:
    if not snapshot:
        return ["- 没有可用的 companyfacts 快照。"]
    derived = snapshot.get("derived_metrics", {})
    lines = [
        f"- 报告期末：`{snapshot.get('period_end', 'n/a')}`；Filed on：`{snapshot.get('filed_on', 'n/a')}`",
        (
            f"- 营收：`{_format_money(snapshot.get('revenue_latest'))}` vs "
            f"`{_format_money(snapshot.get('revenue_previous'))}`；同比 `{_format_pct(derived.get('revenue_growth'))}`"
        ),
        (
            f"- 经营现金流：`{_format_money(snapshot.get('operating_cashflow_latest'))}` vs "
            f"`{_format_money(snapshot.get('operating_cashflow_previous'))}`"
        ),
        (
            f"- 现金 / 债务：`{_format_money(snapshot.get('cash_latest'))}` / "
            f"`{_format_money(snapshot.get('debt_latest'))}`；净现金 `{_format_money(derived.get('cash_minus_debt'))}`"
        ),
        f"- 资本开支：`{_format_money(snapshot.get('capex_latest'))}`",
    ]
    return lines


def _render_social_summary(snapshot: dict[str, Any] | None) -> list[str]:
    if not snapshot:
        return ["- 当前没有可用的社交情绪摘要。"]
    lines = [
        f"- 状态：`{snapshot.get('state', 'n/a')}`；分数 `{snapshot.get('score', 'n/a')}`",
        (
            f"- 最近 `{snapshot.get('recent_window_hours', 'n/a')}` 小时有效帖 `{snapshot.get('recent_posts', 'n/a')}` 条，"
            f"基线期 `{snapshot.get('baseline_posts', 'n/a')}` 条；独立作者 `{snapshot.get('unique_authors', 'n/a')}`"
        ),
        (
            f"- 情绪 delta：`{_format_float(snapshot.get('delta'))}`；breadth：`{_format_float(snapshot.get('breadth'))}`；"
            f"硬负面占比：`{_format_pct(snapshot.get('hard_negative_ratio'))}`"
        ),
    ]
    provider_counts = snapshot.get("provider_post_counts") or {}
    recent_provider_counts = snapshot.get("recent_provider_post_counts") or {}
    source_brief = snapshot.get("source_brief") or []
    if provider_counts:
        lines.append(
            f"- 来源分布：{', '.join(f'{source}:{count}' for source, count in provider_counts.items())}"
        )
    if recent_provider_counts:
        lines.append(
            f"- 最近窗口来源分布：{', '.join(f'{source}:{count}' for source, count in recent_provider_counts.items())}"
        )
    bullish = ", ".join(theme.get("label", "n/a") for theme in snapshot.get("top_bullish_themes", [])) or "n/a"
    bearish = ", ".join(theme.get("label", "n/a") for theme in snapshot.get("top_bearish_themes", [])) or "n/a"
    lines.append(f"- 正面主题：{bullish}")
    lines.append(f"- 负面主题：{bearish}")
    if source_brief:
        lines.extend(
            [
                "",
                "分来源摘要：",
                "| 来源 | 总帖 | 最近 | 作者 | 情绪 | Delta | 硬负面 | 主题 | 代表帖 |",
                "|---|---:|---:|---:|---|---:|---:|---|---|",
            ]
        )
        for item in source_brief:
            theme = item.get("top_bullish_theme") or item.get("top_bearish_theme") or "n/a"
            lines.append(
                "| "
                + " | ".join(
                    [
                        _source_label(item.get("source")),
                        str(item.get("total_posts", "n/a")),
                        str(item.get("recent_posts", "n/a")),
                        str(item.get("unique_authors", "n/a")),
                        str(item.get("overall_tone", "n/a")),
                        _format_float(item.get("delta")),
                        _format_pct(item.get("hard_negative_ratio")),
                        str(theme),
                        str(item.get("representative_title") or "n/a"),
                    ]
                )
                + " |"
            )
    representative_posts = snapshot.get("representative_posts", [])
    if representative_posts:
        lines.append("- 代表性帖子：")
        for post in representative_posts[:3]:
            lines.append(
                f"  - `{post.get('created_at', 'n/a')[:16]}` `{post.get('source', 'n/a')}`/{post.get('community', 'n/a')}: {post.get('title', 'n/a')}"
            )
    return lines


def _render_option_summary(snapshot: dict[str, Any] | None) -> list[str]:
    if not snapshot:
        return ["- 当前没有可用的期权链摘要。"]
    lines = [
        (
            f"- 合约数：`{snapshot.get('contract_count', 'n/a')}`；call / put："
            f"`{snapshot.get('call_contracts', 'n/a')}` / `{snapshot.get('put_contracts', 'n/a')}`"
        ),
        (
            f"- 成交量 put/call 比：`{_format_float(snapshot.get('put_call_volume_ratio'), digits=4)}`；"
            f"未平仓 put/call 比：`{_format_float(snapshot.get('put_call_open_interest_ratio'), digits=4)}`"
        ),
        (
            f"- 平均隐含波动率：`{_format_pct(snapshot.get('implied_volatility_avg'))}`；"
            f"最近到期日：`{snapshot.get('nearest_expiration', 'n/a')}`；剩余 `{snapshot.get('nearest_days_to_expiry', 'n/a')}` 天"
        ),
        (
            f"- call 最大未平仓 strike：`{_format_money(snapshot.get('max_call_open_interest_strike'))}`；"
            f"put 最大未平仓 strike：`{_format_money(snapshot.get('max_put_open_interest_strike'))}`"
        ),
    ]
    top_contracts = snapshot.get("top_contracts") or []
    if top_contracts:
        lines.append("- 重点合约：")
        for contract in top_contracts[:3]:
            lines.append(
                "  - "
                + " | ".join(
                    [
                        str(contract.get("contract_id", "n/a")),
                        str(contract.get("option_type", "n/a")),
                        f"strike `{_format_money(contract.get('strike'))}`",
                        f"vol `{contract.get('volume', 'n/a')}`",
                        f"oi `{contract.get('open_interest', 'n/a')}`",
                        f"last `{_format_money(contract.get('last_price'))}`",
                    ]
                )
            )
    return lines


def _render_source_health(scorecards: list[ScoreCard], packets: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for card in scorecards:
        packet = packets.get(card.security.ticker, {})
        issues = [
            status
            for status in packet.get("source_health", [])
            if status.get("partial") or not status.get("success")
        ]
        issues.extend(
            status
            for status in packet.get("social_source_health", [])
            if status.get("partial") or not status.get("success")
        )
        issues.extend(
            status
            for status in packet.get("options_source_health", [])
            if status.get("partial") or not status.get("success")
        )
        if not issues:
            continue
        lines.append(f"- `{card.security.ticker}`：")
        for issue in issues:
            lines.append(
                f"  - `{issue.get('source', 'unknown')}`：{issue.get('message', 'n/a')}"
            )
    return lines


def _rule_state_reason(card: ScoreCard) -> str:
    if card.veto_reason:
        return f"出现 veto `{card.veto_reason}`，因此直接落到 `Reject`。"
    if card.state == ActionState.REJECT:
        return f"总分 `{card.total_score}` 低于 `65`，还没有达到进入观察名单的门槛。"
    if card.state == ActionState.WATCH and card.trigger.new_low and "fresh_low" in card.trigger.reasons:
        return (
            f"虽然总分 `{card.total_score}` 已经不低，但触发原因里包含 `fresh_low`，"
            "规则会把动作上限压在 `Watch`。"
        )
    if card.state == ActionState.WATCH:
        return f"总分 `{card.total_score}` 落在 `65-71` 区间，因此先进入 `Watch`。"
    if card.state == ActionState.STARTER:
        return f"总分 `{card.total_score}` 落在 `72-79`，且不受 `fresh_low` 上限限制，因此进入 `Starter`。"
    if card.state == ActionState.ADD:
        return f"总分 `{card.total_score}` 达到 `80+`，且不受 `fresh_low` 限制，因此进入 `Add`。"
    return ACTION_DESCRIPTIONS.get(card.state, "")


def _window_compact(window: dict[str, Any] | None) -> str:
    if not window:
        return "n/a"
    return f"{window.get('start_date', 'n/a')} -> {window.get('end_date', 'n/a')} ({_format_pct(window.get('drawdown'))})"


def _window_sentence(window: dict[str, Any] | None, ticker: str, label: str) -> str:
    if not window:
        return f"{label}缺失"
    return (
        f"`{window.get('start_date', 'n/a')}` 到 `{window.get('end_date', 'n/a')}`，"
        f"`{ticker}` 从 `{_format_price(window.get('start_close'))}` 到 `{_format_price(window.get('end_close'))}`，"
        f"跌幅 `{_format_pct(window.get('drawdown'))}`"
    )


def _fundamental_summary(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return "缺少 companyfacts"
    derived = snapshot.get("derived_metrics", {})
    pieces = []
    revenue_growth = derived.get("revenue_growth")
    if revenue_growth is not None:
        pieces.append(f"营收 {revenue_growth:.1%}")
    if snapshot.get("operating_cashflow_latest") is not None:
        pieces.append("OCF 正" if snapshot["operating_cashflow_latest"] > 0 else "OCF 负")
    cash_minus_debt = derived.get("cash_minus_debt")
    if cash_minus_debt is not None:
        pieces.append("净现金" if cash_minus_debt >= 0 else "净负债")
    return " / ".join(pieces) or "有快照，待人工展开"


def _translate_note(note: str) -> str:
    return NOTE_LABELS.get(note, note)


def _source_label(value: Any) -> str:
    normalized = str(value or "unknown")
    return SOURCE_LABELS.get(normalized, normalized)


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}"


def _format_price(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _format_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:.2f}"


def _format_float(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _render_decision_tracking(decision_tracking_result: Any) -> list[str]:
    """
    Render the Position Watch section for decision tracking.

    If decision_tracking_result is None or has no decisions, show "当前无跟踪中的建议。"
    Otherwise, render:
    - Ingest warnings (if any)
    - Fired alerts (if any)
    - Still-active decisions in a compact table
    """
    lines: list[str] = []

    if decision_tracking_result is None:
        lines.append("- 当前无跟踪中的建议。")
        return lines

    ingest_warnings = decision_tracking_result.ingest_warnings or []
    alerts = decision_tracking_result.alerts or []
    active_summaries = decision_tracking_result.active_summaries or []

    if not ingest_warnings and not alerts and not active_summaries:
        lines.append("- 当前无跟踪中的建议。")
        return lines

    # Render ingest warnings if present
    if ingest_warnings:
        lines.append("### 决策文件问题")
        for warning in ingest_warnings:
            lines.append(f"- ⚠️ {warning}")
        lines.append("")

    # Render fired alerts
    if alerts:
        lines.append("### 已触发的决策")
        for alert in alerts:
            kind_label = {
                "invalidated": "失效",
                "rerated": "重评",
                "expired": "到期",
            }.get(alert.kind, alert.kind)
            lines.append(f"- `{alert.ticker}` ({kind_label})：{alert.reason}")
        lines.append("")

    # Render still-active decisions in a table
    if active_summaries:
        lines.append("### 监控中的建议")
        lines.append("")
        lines.append(
            "| Ticker | 建议状态 | 决策日期 | 失效条件 | 重评条件 | 状态 |"
        )
        lines.append("|---|---|---|---|---|---|")
        for summary in active_summaries:
            ticker = summary.get("ticker", "?")
            state = summary.get("state", "?")
            decision_date = summary.get("decision_date", "?")
            invalidate_strs = summary.get("invalidate_conditions", [])
            rerate_strs = summary.get("rerate_conditions", [])
            status = summary.get("status", "?")

            invalidate_text = "; ".join(invalidate_strs) if invalidate_strs else "无"
            rerate_text = "; ".join(rerate_strs) if rerate_strs else "无"

            lines.append(
                f"| `{ticker}` | `{state}` | `{decision_date}` | {invalidate_text} | {rerate_text} | {status} |"
            )

    return lines
