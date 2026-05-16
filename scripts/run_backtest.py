#!/usr/bin/env python3
"""CLI: run the market-sentiment strategy backtest.

Reads the dataset built by build_backtest_dataset.py, replays the rule engine
to produce daily statuses, simulates a portfolio, and writes a report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sharing-resources" / "src"))

from market_sentiment.backtest.runner import run_backtest  # noqa: E402
from market_sentiment.backtest.simulator import BacktestConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the market-sentiment strategy backtest.")
    parser.add_argument("--dataset-dir", default="data/backtest/dataset")
    parser.add_argument("--output-dir", default="data/backtest/results")
    parser.add_argument("--config", default="config/watchlist.toml")
    parser.add_argument("--start", default=None, help="decision window start (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="decision window end (YYYY-MM-DD)")
    parser.add_argument("--starting-cash", type=float, default=100_000.0)
    parser.add_argument("--position-dollars", type=float, default=10_000.0)
    parser.add_argument("--max-adds", type=int, default=2)
    parser.add_argument("--commission", type=float, default=2.0)
    parser.add_argument("--take-profit", type=float, default=0.25)
    parser.add_argument("--stop-loss", type=float, default=0.12)
    parser.add_argument("--max-holding-days", type=int, default=60)
    parser.add_argument("--no-signal-exit", action="store_true", dest="no_signal_exit",
                        help="Disable signal-driven exits")
    args = parser.parse_args()

    sim_config = BacktestConfig(
        starting_cash=args.starting_cash,
        position_dollars=args.position_dollars,
        max_adds=args.max_adds,
        commission_per_trade=args.commission,
        take_profit_pct=args.take_profit,
        stop_loss_pct=args.stop_loss,
        max_holding_days=args.max_holding_days,
        signal_exit_enabled=not args.no_signal_exit,
    )

    summary = run_backtest(
        dataset_dir=args.dataset_dir,
        output_dir=args.output_dir,
        config_path=args.config,
        start_date=args.start,
        end_date=args.end,
        sim_config=sim_config,
    )

    print("=== 回测完成 ===")
    print(f"期末权益:   ${summary['final_equity']:,.2f}")
    print(f"总收益率:   {summary['total_return_pct']:.2f}%")
    print(f"最大回撤:   {summary['max_drawdown_pct']:.2f}%")
    print(f"平仓笔数:   {summary['num_positions']}  (胜率 {summary['win_rate']:.1f}%)")
    print(f"总手续费:   ${summary['total_commission']:,.2f}")
    print(f"产物目录:   {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
