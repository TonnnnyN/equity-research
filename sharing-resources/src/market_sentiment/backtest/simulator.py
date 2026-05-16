"""
Daily portfolio simulator for stock backtesting.

Consumes per-day stock status signals (from rule engine), applies entry/exit rules,
charges commission, and produces trades + equity curve + summary.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import math


@dataclass
class BacktestConfig:
    """Configuration for backtest simulation."""
    starting_cash: float = 100_000.0
    position_dollars: float = 10_000.0
    max_adds: int = 2
    commission_per_trade: float = 2.0
    take_profit_pct: float = 0.25
    stop_loss_pct: float = 0.12
    max_holding_days: int = 60
    signal_exit_enabled: bool = True
    entry_states: tuple = field(default_factory=lambda: ("Starter", "Add"))


@dataclass
class BacktestResult:
    """Result of a backtest run."""
    trades: list[dict]
    equity_curve: list[dict]
    closed_positions: list[dict]
    summary: dict

    def to_dict(self) -> dict:
        """Return all results as a dict."""
        return {
            "trades": self.trades,
            "equity_curve": self.equity_curve,
            "closed_positions": self.closed_positions,
            "summary": self.summary,
        }


def run_simulation(
    statuses: list[dict],
    price_bars: dict[str, list[dict]],
    config: Optional[BacktestConfig] = None,
) -> BacktestResult:
    """
    Run a daily portfolio simulation.

    Args:
        statuses: List of status records, each with date, ticker, state, etc.
        price_bars: Dict mapping ticker to sorted list of price bars.
        config: BacktestConfig (uses defaults if None).

    Returns:
        BacktestResult with trades, equity_curve, closed_positions, summary.
    """
    if config is None:
        config = BacktestConfig()

    # Build the trading calendar: sorted union of all dates across all bars
    all_dates = set()
    for ticker, bars in price_bars.items():
        for bar in bars:
            all_dates.add(bar["date"])

    trading_calendar = sorted(all_dates)
    if not trading_calendar:
        # No trading days; return empty result
        return BacktestResult(
            trades=[],
            equity_curve=[],
            closed_positions=[],
            summary=_make_summary([], [], [], config),
        )

    # Build date-to-index map for calendar
    date_to_index = {date: idx for idx, date in enumerate(trading_calendar)}

    # Build price bar lookup: ticker -> date -> bar
    bars_by_date = {}
    for ticker, bars in price_bars.items():
        bars_by_date[ticker] = {bar["date"]: bar for bar in bars}

    # State tracking
    cash = config.starting_cash
    positions = {}  # ticker -> Position
    trades = []
    closed_positions = []
    equity_curve = []

    # Map statuses by date for fast lookup
    statuses_by_date = {}
    for status in statuses:
        date = status["date"]
        if date not in statuses_by_date:
            statuses_by_date[date] = []
        statuses_by_date[date].append(status)

    # Main simulation loop
    for day_idx, current_date in enumerate(trading_calendar):
        # (1) EXITS first
        # Compute exit_signal_tickers from previous date's statuses
        previous_date = None
        if day_idx > 0:
            previous_date = trading_calendar[day_idx - 1]

        exit_signal_tickers = set()
        if config.signal_exit_enabled and previous_date is not None:
            prev_statuses = statuses_by_date.get(previous_date, [])
            for status in prev_statuses:
                if status["state"] == "Reject" or status.get("veto_reason"):
                    exit_signal_tickers.add(status["ticker"])

        tickers_to_exit = []
        for ticker, position in positions.items():
            if ticker not in bars_by_date or current_date not in bars_by_date[ticker]:
                continue

            bar = bars_by_date[ticker][current_date]
            days_held = day_idx - date_to_index[position["first_buy_date"]]
            avg_cost = position["cost_basis"] / position["shares"]
            stop_price = avg_cost * (1 - config.stop_loss_pct)
            tp_price = avg_cost * (1 + config.take_profit_pct)

            exit_reason = None
            fill_price = None

            # Priority: stop loss, take profit, signal exit, max hold
            if bar["low"] <= stop_price:
                exit_reason = "stop_loss"
                fill_price = min(stop_price, bar["open"])
            elif bar["high"] >= tp_price:
                exit_reason = "take_profit"
                fill_price = max(tp_price, bar["open"])
            elif config.signal_exit_enabled and ticker in exit_signal_tickers:
                exit_reason = "signal_exit"
                fill_price = bar["open"]
            elif days_held >= config.max_holding_days:
                exit_reason = "max_holding"
                fill_price = bar["open"]

            if exit_reason:
                tickers_to_exit.append((ticker, exit_reason, fill_price))

        # Execute exits
        for ticker, exit_reason, fill_price in tickers_to_exit:
            position = positions[ticker]
            proceeds = position["shares"] * fill_price
            cash += proceeds - config.commission_per_trade

            # Record trade (sell)
            trades.append({
                "date": current_date,
                "ticker": ticker,
                "action": "sell",
                "shares": position["shares"],
                "price": round(fill_price, 4),
                "commission": config.commission_per_trade,
                "reason": exit_reason,
                "cash_after": round(cash, 2),
            })

            # Record closed position
            avg_cost = position["cost_basis"] / position["shares"]
            gross_pnl = position["shares"] * (fill_price - avg_cost)
            commission_total = position["commission_total"] + config.commission_per_trade
            net_pnl = gross_pnl - commission_total
            return_pct = net_pnl / position["cost_basis"] if position["cost_basis"] > 0 else 0
            holding_days = day_idx - date_to_index[position["first_buy_date"]]

            closed_positions.append({
                "ticker": ticker,
                "entry_date": position["first_buy_date"],
                "exit_date": current_date,
                "exit_reason": exit_reason,
                "shares": position["shares"],
                "avg_cost": round(avg_cost, 4),
                "exit_price": round(fill_price, 4),
                "buy_count": position["buy_count"],
                "gross_pnl": round(gross_pnl, 2),
                "commission_total": round(commission_total, 2),
                "net_pnl": round(net_pnl, 2),
                "return_pct": round(return_pct, 4),
                "holding_days": holding_days,
            })

            del positions[ticker]

        # (2) ENTRIES
        previous_date_idx = day_idx - 1
        if previous_date_idx >= 0:
            previous_date = trading_calendar[previous_date_idx]
            entry_statuses = statuses_by_date.get(previous_date, [])

            # Dedupe: prefer "Add" over "Starter"
            statuses_by_ticker = {}
            for status in entry_statuses:
                ticker = status["ticker"]
                if ticker not in statuses_by_ticker:
                    statuses_by_ticker[ticker] = status
                else:
                    # Prefer "Add"
                    if status["state"] == "Add":
                        statuses_by_ticker[ticker] = status

            for ticker, status in statuses_by_ticker.items():
                # Skip if state is Reject or has veto_reason
                if status["state"] == "Reject" or status.get("veto_reason"):
                    continue

                # Skip if state not in entry_states
                if status["state"] not in config.entry_states:
                    continue

                # Get bar for current_date
                if ticker not in bars_by_date or current_date not in bars_by_date[ticker]:
                    continue

                bar = bars_by_date[ticker][current_date]
                buy_price = bar["open"]
                if buy_price <= 0:
                    continue

                holding = ticker in positions
                state = status["state"]

                if not holding:
                    # FLAT: open new position
                    shares = int(buy_price and config.position_dollars / buy_price or 0)
                    if shares < 1:
                        continue
                    cost = shares * buy_price
                    if cash < cost + config.commission_per_trade:
                        continue

                    cash -= cost + config.commission_per_trade
                    positions[ticker] = {
                        "ticker": ticker,
                        "shares": shares,
                        "cost_basis": cost,
                        "buy_count": 1,
                        "first_buy_date": current_date,
                        "buy_trade_indexes": [len(trades)],
                        "commission_total": config.commission_per_trade,
                    }

                    trades.append({
                        "date": current_date,
                        "ticker": ticker,
                        "action": "buy",
                        "shares": shares,
                        "price": round(buy_price, 4),
                        "commission": config.commission_per_trade,
                        "reason": "starter",
                        "cash_after": round(cash, 2),
                    })

                elif state == "Add" and positions[ticker]["buy_count"] < 1 + config.max_adds:
                    # ADD to existing position
                    position = positions[ticker]
                    shares = int(config.position_dollars / buy_price)
                    if shares < 1:
                        continue
                    cost = shares * buy_price
                    if cash < cost + config.commission_per_trade:
                        continue

                    cash -= cost + config.commission_per_trade
                    position["shares"] += shares
                    position["cost_basis"] += cost
                    position["buy_count"] += 1
                    position["commission_total"] += config.commission_per_trade
                    position["buy_trade_indexes"].append(len(trades))

                    trades.append({
                        "date": current_date,
                        "ticker": ticker,
                        "action": "buy",
                        "shares": shares,
                        "price": round(buy_price, 4),
                        "commission": config.commission_per_trade,
                        "reason": "add",
                        "cash_after": round(cash, 2),
                    })

        # (3) MARK TO MARKET at end of day
        positions_value = 0
        for ticker, position in positions.items():
            # Find latest bar on or before current_date
            if ticker not in bars_by_date:
                continue
            latest_bar = None
            for search_idx in range(day_idx, -1, -1):
                search_date = trading_calendar[search_idx]
                if search_date in bars_by_date[ticker]:
                    latest_bar = bars_by_date[ticker][search_date]
                    break

            if latest_bar:
                positions_value += position["shares"] * latest_bar["close"]

        equity = cash + positions_value
        equity_curve.append({
            "date": current_date,
            "cash": round(cash, 2),
            "positions_value": round(positions_value, 2),
            "equity": round(equity, 2),
        })

    # Liquidate remaining positions at end of backtest
    for ticker in list(positions.keys()):
        position = positions[ticker]
        # Get last available close for this ticker
        if ticker not in bars_by_date or not bars_by_date[ticker]:
            continue

        # Find the last bar
        last_bar = None
        for search_date in reversed(trading_calendar):
            if search_date in bars_by_date[ticker]:
                last_bar = bars_by_date[ticker][search_date]
                break

        if last_bar:
            exit_price = last_bar["close"]
            proceeds = position["shares"] * exit_price
            cash += proceeds - config.commission_per_trade

            # Record trade
            trades.append({
                "date": trading_calendar[-1],
                "ticker": ticker,
                "action": "sell",
                "shares": position["shares"],
                "price": round(exit_price, 4),
                "commission": config.commission_per_trade,
                "reason": "backtest_end",
                "cash_after": round(cash, 2),
            })

            # Record closed position
            avg_cost = position["cost_basis"] / position["shares"]
            gross_pnl = position["shares"] * (exit_price - avg_cost)
            commission_total = position["commission_total"] + config.commission_per_trade
            net_pnl = gross_pnl - commission_total
            return_pct = net_pnl / position["cost_basis"] if position["cost_basis"] > 0 else 0
            holding_days = date_to_index[trading_calendar[-1]] - date_to_index[position["first_buy_date"]]

            closed_positions.append({
                "ticker": ticker,
                "entry_date": position["first_buy_date"],
                "exit_date": trading_calendar[-1],
                "exit_reason": "backtest_end",
                "shares": position["shares"],
                "avg_cost": round(avg_cost, 4),
                "exit_price": round(exit_price, 4),
                "buy_count": position["buy_count"],
                "gross_pnl": round(gross_pnl, 2),
                "commission_total": round(commission_total, 2),
                "net_pnl": round(net_pnl, 2),
                "return_pct": round(return_pct, 4),
                "holding_days": holding_days,
            })

    summary = _make_summary(trades, closed_positions, equity_curve, config)

    return BacktestResult(
        trades=trades,
        equity_curve=equity_curve,
        closed_positions=closed_positions,
        summary=summary,
    )


def _make_summary(
    trades: list[dict],
    closed_positions: list[dict],
    equity_curve: list[dict],
    config: BacktestConfig,
) -> dict:
    """Compute summary statistics."""
    num_positions = len(closed_positions)
    num_buy_trades = sum(1 for t in trades if t["action"] == "buy")
    num_sell_trades = sum(1 for t in trades if t["action"] == "sell")

    num_wins = sum(1 for cp in closed_positions if cp["net_pnl"] > 0)
    num_losses = sum(1 for cp in closed_positions if cp["net_pnl"] < 0)
    win_rate = (num_wins / num_positions * 100) if num_positions > 0 else 0

    total_pnl = sum(cp["net_pnl"] for cp in closed_positions)
    total_commission = sum(t["commission"] for t in trades)
    final_equity = config.starting_cash + total_pnl
    total_return_pct = (total_pnl / config.starting_cash * 100) if config.starting_cash > 0 else 0

    avg_win = (
        sum(cp["net_pnl"] for cp in closed_positions if cp["net_pnl"] > 0) / num_wins
        if num_wins > 0
        else 0
    )
    avg_loss = (
        sum(cp["net_pnl"] for cp in closed_positions if cp["net_pnl"] < 0) / num_losses
        if num_losses > 0
        else 0
    )

    # Compute max drawdown: largest peak-to-trough drop of the equity curve.
    max_drawdown_pct = _compute_max_drawdown(equity_curve)

    return {
        "starting_cash": round(config.starting_cash, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 4),
        "total_pnl": round(total_pnl, 2),
        "num_positions": num_positions,
        "num_wins": num_wins,
        "num_losses": num_losses,
        "win_rate": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "total_commission": round(total_commission, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "num_buy_trades": num_buy_trades,
        "num_sell_trades": num_sell_trades,
    }


def _compute_max_drawdown(equity_curve: list[dict]) -> float:
    """Largest peak-to-trough equity drop, as a positive percentage."""
    peak = None
    max_dd = 0.0
    for point in equity_curve:
        equity = point["equity"]
        if peak is None or equity > peak:
            peak = equity
        if peak and peak > 0:
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return max_dd
