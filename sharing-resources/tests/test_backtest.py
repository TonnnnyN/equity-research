"""Tests for the backtest subsystem (asof_fundamentals, simulator, status_engine)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from market_sentiment.backtest.asof_fundamentals import (
    build_fundamentals_timeline,
    extract_snapshot_asof,
    snapshot_asof,
)
from market_sentiment.backtest.simulator import BacktestConfig, run_simulation
from market_sentiment.backtest.status_engine import compute_statuses, load_raw_price_bars


# --------------------------------------------------------------------------
# asof_fundamentals
# --------------------------------------------------------------------------

def _companyfacts_fixture() -> dict:
    """Two annual revenue filings: FY2023 filed 2024-02-01, FY2024 filed 2025-02-01."""
    return {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K", "fy": 2023, "fp": "FY",
                                "end": "2023-12-31", "filed": "2024-02-01", "val": 1000,
                            },
                            {
                                "form": "10-K", "fy": 2024, "fp": "FY",
                                "end": "2024-12-31", "filed": "2025-02-01", "val": 1200,
                            },
                        ]
                    }
                }
            }
        }
    }


def test_extract_snapshot_asof_respects_cutoff():
    payload = _companyfacts_fixture()
    # Before any filing: nothing available.
    assert extract_snapshot_asof(payload, "AAA", "0000000001", date(2024, 1, 1)) is None
    # After first filing only.
    snap = extract_snapshot_asof(payload, "AAA", "0000000001", date(2024, 6, 1))
    assert snap is not None
    assert snap.revenue_latest == 1000
    # After second filing: latest is FY2024, previous FY2023.
    snap2 = extract_snapshot_asof(payload, "AAA", "0000000001", date(2025, 6, 1))
    assert snap2.revenue_latest == 1200
    assert snap2.revenue_previous == 1000


def test_build_and_lookup_fundamentals_timeline():
    payload = _companyfacts_fixture()
    timeline = build_fundamentals_timeline(payload, "AAA", "0000000001")
    assert len(timeline) == 2
    assert snapshot_asof(timeline, date(2023, 1, 1)) is None
    assert snapshot_asof(timeline, date(2024, 6, 1)).revenue_latest == 1000
    assert snapshot_asof(timeline, date(2025, 6, 1)).revenue_latest == 1200


# --------------------------------------------------------------------------
# simulator
# --------------------------------------------------------------------------

def _bar(d: str, o: float, h: float, l: float, c: float) -> dict:
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": 1000.0}


def test_simulator_take_profit_and_commission():
    bars = {"AAA": [
        _bar("2025-01-01", 100, 101, 99, 100),
        _bar("2025-01-02", 100, 105, 100, 104),
        _bar("2025-01-03", 130, 135, 128, 132),  # gap up above tp
    ]}
    statuses = [{"date": "2025-01-01", "ticker": "AAA", "triggered": True,
                 "state": "Starter", "total_score": 80, "veto_reason": None,
                 "data_insufficient": False}]
    result = run_simulation(statuses, bars, BacktestConfig())
    assert [t["action"] for t in result.trades] == ["buy", "sell"]
    buy, sell = result.trades
    assert buy["date"] == "2025-01-02" and buy["price"] == 100
    # tp_price = 125, but day3 opens at 130 -> fill at the better open price.
    assert sell["reason"] == "take_profit" and sell["price"] == 130
    assert sell["commission"] == 2.0
    assert result.summary["total_commission"] == 4.0
    assert result.summary["num_wins"] == 1


def test_simulator_stop_loss_priority():
    bars = {"AAA": [
        _bar("2025-01-01", 100, 101, 99, 100),
        _bar("2025-01-02", 100, 101, 100, 100),
        _bar("2025-01-03", 100, 130, 80, 95),  # hits both tp-high and sl-low
    ]}
    statuses = [{"date": "2025-01-01", "ticker": "AAA", "triggered": True,
                 "state": "Starter", "total_score": 80, "veto_reason": None,
                 "data_insufficient": False}]
    result = run_simulation(statuses, bars, BacktestConfig())
    assert result.closed_positions[0]["exit_reason"] == "stop_loss"


def test_simulator_max_holding_exit():
    days = [_bar(f"2025-{m:02d}-{d:02d}", 100, 100.5, 99.5, 100)
            for m in range(1, 7) for d in range(1, 29)]
    bars = {"AAA": days}
    statuses = [{"date": days[0]["date"], "ticker": "AAA", "triggered": True,
                 "state": "Starter", "total_score": 80, "veto_reason": None,
                 "data_insufficient": False}]
    result = run_simulation(statuses, bars, BacktestConfig(max_holding_days=10))
    assert result.closed_positions[0]["exit_reason"] == "max_holding"
    assert result.closed_positions[0]["holding_days"] == 10


def test_simulator_signal_exit_on_reject():
    bars = {"AAA": [
        _bar("2025-01-01", 100, 100.5, 99.5, 100),
        _bar("2025-01-02", 100, 100.5, 99.5, 100),
        _bar("2025-01-03", 100, 100.5, 99.5, 100),
        _bar("2025-01-04", 100, 100.5, 99.5, 100),
    ]}
    statuses = [
        {"date": "2025-01-01", "ticker": "AAA", "triggered": True,
         "state": "Starter", "total_score": 80, "veto_reason": None,
         "data_insufficient": False},
        # On 2025-01-03, provide a Reject status -> triggers exit on 2025-01-04 open
        {"date": "2025-01-03", "ticker": "AAA", "triggered": True,
         "state": "Reject", "total_score": 40, "veto_reason": None,
         "data_insufficient": False},
    ]
    result = run_simulation(statuses, bars, BacktestConfig())
    assert len(result.closed_positions) == 1
    assert result.closed_positions[0]["exit_reason"] == "signal_exit"
    assert result.closed_positions[0]["exit_date"] == "2025-01-04"
    assert result.closed_positions[0]["exit_price"] == 100  # filled at open


def test_simulator_signal_exit_on_veto_reason():
    bars = {"AAA": [
        _bar("2025-01-01", 100, 100.5, 99.5, 100),
        _bar("2025-01-02", 100, 100.5, 99.5, 100),
        _bar("2025-01-03", 100, 100.5, 99.5, 100),
        _bar("2025-01-04", 100, 100.5, 99.5, 100),
    ]}
    statuses = [
        {"date": "2025-01-01", "ticker": "AAA", "triggered": True,
         "state": "Watch", "total_score": 60, "veto_reason": None,
         "data_insufficient": False},
        # On 2025-01-02, enter via Starter
        {"date": "2025-01-02", "ticker": "AAA", "triggered": True,
         "state": "Starter", "total_score": 80, "veto_reason": None,
         "data_insufficient": False},
        # On 2025-01-03, provide a status with veto_reason -> exit on 2025-01-04 open
        {"date": "2025-01-03", "ticker": "AAA", "triggered": True,
         "state": "Watch", "total_score": 50, "veto_reason": "thesis_broken",
         "data_insufficient": False},
    ]
    result = run_simulation(statuses, bars, BacktestConfig())
    assert len(result.closed_positions) == 1
    assert result.closed_positions[0]["exit_reason"] == "signal_exit"
    assert result.closed_positions[0]["exit_date"] == "2025-01-04"


def test_simulator_signal_exit_disabled():
    bars = {"AAA": [
        _bar("2025-01-01", 100, 100.5, 99.5, 100),
        _bar("2025-01-02", 100, 100.5, 99.5, 100),
        _bar("2025-01-03", 100, 100.5, 99.5, 100),
        _bar("2025-01-04", 100, 100.5, 99.5, 100),
        _bar("2025-01-05", 100, 100.5, 99.5, 100),
    ]}
    statuses = [
        {"date": "2025-01-01", "ticker": "AAA", "triggered": True,
         "state": "Starter", "total_score": 80, "veto_reason": None,
         "data_insufficient": False},
        # On 2025-01-03, provide a Reject status, but signal_exit_enabled=False
        {"date": "2025-01-03", "ticker": "AAA", "triggered": True,
         "state": "Reject", "total_score": 40, "veto_reason": None,
         "data_insufficient": False},
    ]
    # With signal_exit_enabled=False, the position should NOT exit via signal
    result = run_simulation(statuses, bars, BacktestConfig(signal_exit_enabled=False))
    # Position should still be open at end of backtest
    assert len(result.closed_positions) == 1
    # It should exit only via backtest_end, not signal_exit
    assert result.closed_positions[0]["exit_reason"] == "backtest_end"
    assert result.closed_positions[0]["exit_date"] == "2025-01-05"


def test_simulator_reject_does_not_enter():
    bars = {"AAA": [_bar("2025-01-01", 100, 101, 99, 100),
                    _bar("2025-01-02", 100, 101, 99, 100)]}
    statuses = [{"date": "2025-01-01", "ticker": "AAA", "triggered": True,
                 "state": "Reject", "total_score": 40, "veto_reason": None,
                 "data_insufficient": False}]
    result = run_simulation(statuses, bars, BacktestConfig())
    assert result.trades == []


def test_simulator_add_increases_position():
    bars = {"AAA": [_bar(f"2025-01-{d:02d}", 100, 101, 99, 100) for d in range(1, 11)]}
    statuses = [
        {"date": "2025-01-01", "ticker": "AAA", "triggered": True, "state": "Starter",
         "total_score": 80, "veto_reason": None, "data_insufficient": False},
        {"date": "2025-01-03", "ticker": "AAA", "triggered": True, "state": "Add",
         "total_score": 85, "veto_reason": None, "data_insufficient": False},
    ]
    result = run_simulation(statuses, bars, BacktestConfig())
    buys = [t for t in result.trades if t["action"] == "buy"]
    assert len(buys) == 2
    assert result.closed_positions[0]["buy_count"] == 2


def test_simulator_empty_inputs():
    result = run_simulation([], {}, BacktestConfig())
    assert result.trades == []
    assert result.summary["num_positions"] == 0


# --------------------------------------------------------------------------
# status_engine
# --------------------------------------------------------------------------

def _write_price_series(path, ticker, start, count, start_close, daily_change):
    bars = []
    d = start
    close = start_close
    added = 0
    while added < count:
        if d.weekday() < 5:
            bars.append({
                "date": d.isoformat(),
                "open": round(close, 4), "high": round(close * 1.01, 4),
                "low": round(close * 0.99, 4), "close": round(close, 4),
                "volume": 1_000_000.0,
            })
            close *= (1 + daily_change)
            added += 1
        d += timedelta(days=1)
    path.write_text(json.dumps({"ticker": ticker, "source": "yahoo_chart", "bars": bars}))


def test_status_engine_produces_triggered_status(tmp_path):
    dataset = tmp_path / "dataset"
    prices = dataset / "prices"
    prices.mkdir(parents=True)
    # NVDA drops ~2%/day for 40 days; SOXX benchmark flat -> trigger fires.
    _write_price_series(prices / "NVDA.json", "NVDA", date(2025, 1, 1), 40, 200.0, -0.02)
    _write_price_series(prices / "SOXX.json", "SOXX", date(2025, 1, 1), 40, 200.0, 0.0)

    statuses = compute_statuses(dataset, config_path="config/watchlist.toml")
    nvda = [s for s in statuses if s["ticker"] == "NVDA"]
    assert nvda, "expected NVDA to trigger on a sustained drawdown"
    for s in nvda:
        assert s["triggered"] is True
        assert s["state"] in {"Reject", "Watch", "Starter", "Add"}
        assert "ten_day_drawdown" in s["trigger_reasons"]


def test_status_engine_as_of_filtering(tmp_path):
    """A status on date D must only use price history up to D."""
    dataset = tmp_path / "dataset"
    prices = dataset / "prices"
    prices.mkdir(parents=True)
    _write_price_series(prices / "NVDA.json", "NVDA", date(2025, 1, 1), 60, 200.0, -0.02)
    _write_price_series(prices / "SOXX.json", "SOXX", date(2025, 1, 1), 60, 200.0, 0.0)

    statuses = compute_statuses(dataset, config_path="config/watchlist.toml")
    raw = load_raw_price_bars(dataset)
    for s in (x for x in statuses if x["ticker"] == "NVDA"):
        bar_on_date = next(b for b in raw["NVDA"] if b["date"] == s["date"])
        assert s["close"] == pytest.approx(bar_on_date["close"], rel=1e-6)
