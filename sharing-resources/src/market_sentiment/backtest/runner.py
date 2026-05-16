"""Backtest runner: dataset -> statuses -> simulation -> report.

Ties the backtest subsystem together and writes all artifacts (statuses,
trades, equity curve, summary, markdown report) under an output directory.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from market_sentiment.backtest.simulator import BacktestConfig, run_simulation
from market_sentiment.backtest.status_engine import compute_statuses, load_raw_price_bars


def _parse_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def _benchmark_return(bars: list[dict], start: str, end: str) -> float | None:
    """Buy-and-hold return of a price series over [start, end], as a fraction."""
    window = [b for b in bars if start <= b["date"] <= end]
    if len(window) < 2 or not window[0]["close"]:
        return None
    return window[-1]["close"] / window[0]["close"] - 1.0


def run_backtest(
    dataset_dir: str | Path,
    output_dir: str | Path,
    config_path: str = "config/watchlist.toml",
    start_date: str | None = None,
    end_date: str | None = None,
    sim_config: BacktestConfig | None = None,
) -> dict:
    """Run the full backtest and write artifacts. Returns the summary dict."""
    dataset_dir = Path(dataset_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sim_config = sim_config or BacktestConfig()

    statuses = compute_statuses(
        dataset_dir, config_path=config_path, start_date=start_date, end_date=end_date
    )
    raw_bars = load_raw_price_bars(dataset_dir)

    # Decision window: from the statuses, fall back to dataset extent.
    status_dates = sorted({s["date"] for s in statuses})
    window_start = start_date or (status_dates[0] if status_dates else None)
    if window_start is None:
        all_dates = sorted({b["date"] for bars in raw_bars.values() for b in bars})
        window_start = all_dates[0] if all_dates else None

    # Restrict the simulation calendar to the decision window so the sim does
    # not idle through the warmup period.
    sim_bars = {
        ticker: [b for b in bars if window_start is None or b["date"] >= window_start]
        for ticker, bars in raw_bars.items()
    }
    sim_bars = {t: b for t, b in sim_bars.items() if b}

    result = run_simulation(statuses, sim_bars, sim_config)

    window_end = end_date
    if window_end is None:
        sim_dates = sorted({b["date"] for bars in sim_bars.values() for b in bars})
        window_end = sim_dates[-1] if sim_dates else window_start

    # Artifacts.
    (output_dir / "statuses.json").write_text(
        json.dumps(statuses, indent=2), encoding="utf-8"
    )
    (output_dir / "trades.json").write_text(
        json.dumps(result.trades, indent=2), encoding="utf-8"
    )
    (output_dir / "summary.json").write_text(
        json.dumps(result.summary, indent=2), encoding="utf-8"
    )
    _write_csv(output_dir / "trades.csv", result.trades)
    _write_csv(output_dir / "equity_curve.csv", result.equity_curve)
    _write_csv(output_dir / "closed_positions.csv", result.closed_positions)

    benchmarks = {
        b: _benchmark_return(raw_bars[b], window_start, window_end)
        for b in ("QQQ", "SOXX", "XLU", "PPH")
        if b in raw_bars
    }
    report = _render_report(
        result, statuses, sim_config, window_start, window_end, benchmarks
    )
    (output_dir / "report.md").write_text(report, encoding="utf-8")

    return result.summary


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _render_report(
    result,
    statuses: list[dict],
    cfg: BacktestConfig,
    window_start: str | None,
    window_end: str | None,
    benchmarks: dict[str, float | None],
) -> str:
    s = result.summary
    lines: list[str] = []
    lines.append("# 市场情绪策略回测报告")
    lines.append("")
    lines.append(f"- 回测区间: {window_start} ~ {window_end}")
    lines.append(f"- 初始资金: ${cfg.starting_cash:,.2f}")
    lines.append(f"- 每次买入金额: ${cfg.position_dollars:,.2f}（每个标的最多 {1 + cfg.max_adds} 次买入）")
    lines.append(f"- 手续费: 买入/卖出每笔 ${cfg.commission_per_trade:.2f}")
    lines.append(f"- 止盈/止损: +{cfg.take_profit_pct*100:.0f}% / -{cfg.stop_loss_pct*100:.0f}%")
    lines.append(f"- 最长持有: {cfg.max_holding_days} 个交易日")
    lines.append("")
    lines.append("## 总体结果")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 期末权益 | ${s['final_equity']:,.2f} |")
    lines.append(f"| 总收益率 | {s['total_return_pct']:.2f}% |")
    lines.append(f"| 净盈亏 | ${s['total_pnl']:,.2f} |")
    lines.append(f"| 最大回撤 | {s['max_drawdown_pct']:.2f}% |")
    lines.append(f"| 平仓笔数 | {s['num_positions']} |")
    lines.append(f"| 胜 / 负 | {s['num_wins']} / {s['num_losses']} |")
    lines.append(f"| 胜率 | {s['win_rate']:.1f}% |")
    lines.append(f"| 平均盈利 / 平均亏损 | ${s['avg_win']:,.2f} / ${s['avg_loss']:,.2f} |")
    lines.append(f"| 买入 / 卖出 笔数 | {s['num_buy_trades']} / {s['num_sell_trades']} |")
    lines.append(f"| 总手续费 | ${s['total_commission']:,.2f} |")
    lines.append("")

    lines.append("## 基准对比（同期买入持有）")
    lines.append("")
    lines.append("| 基准 | 区间收益率 |")
    lines.append("|---|---|")
    for name, ret in benchmarks.items():
        lines.append(f"| {name} | {ret*100:.2f}% |" if ret is not None else f"| {name} | N/A |")
    lines.append("")

    state_counts = Counter(st["state"] for st in statuses)
    lines.append("## 信号分布（触发后的状态）")
    lines.append("")
    lines.append("| 状态 | 次数 |")
    lines.append("|---|---|")
    for st_name in ("Add", "Starter", "Watch", "Reject"):
        lines.append(f"| {st_name} | {state_counts.get(st_name, 0)} |")
    lines.append(f"| 触发信号总数 | {len(statuses)} |")
    lines.append("")

    exit_counts = Counter(cp["exit_reason"] for cp in result.closed_positions)
    if exit_counts:
        lines.append("## 平仓原因分布")
        lines.append("")
        lines.append("| 原因 | 次数 |")
        lines.append("|---|---|")
        for reason, count in exit_counts.most_common():
            lines.append(f"| {reason} | {count} |")
        lines.append("")

    closed = sorted(result.closed_positions, key=lambda c: c["net_pnl"], reverse=True)
    if closed:
        lines.append("## 盈利前 5 / 亏损前 5")
        lines.append("")
        lines.append("| 标的 | 入场 | 出场 | 原因 | 净盈亏 | 收益率 |")
        lines.append("|---|---|---|---|---|---|")
        for cp in closed[:5]:
            lines.append(
                f"| {cp['ticker']} | {cp['entry_date']} | {cp['exit_date']} | "
                f"{cp['exit_reason']} | ${cp['net_pnl']:,.2f} | {cp['return_pct']*100:.1f}% |"
            )
        for cp in closed[-5:][::-1]:
            lines.append(
                f"| {cp['ticker']} | {cp['entry_date']} | {cp['exit_date']} | "
                f"{cp['exit_reason']} | ${cp['net_pnl']:,.2f} | {cp['return_pct']*100:.1f}% |"
            )
        lines.append("")

    lines.append(f"生成时间: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    return "\n".join(lines)
