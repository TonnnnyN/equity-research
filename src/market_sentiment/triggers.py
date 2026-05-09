from __future__ import annotations

from market_sentiment.models import PriceBar, PriceWindow, Threshold, TriggerResult


def compute_trigger(prices: list[PriceBar], benchmark_prices: list[PriceBar], threshold: Threshold) -> TriggerResult:
    if len(prices) < 21 or len(benchmark_prices) < 21:
        return TriggerResult(triggered=False, reasons=["insufficient_price_history"])

    closes = [bar.close for bar in prices]
    last_close = closes[-1]
    ten_day_window = _build_window(prices, 11)
    twenty_day_window = _build_window(prices, 21)
    benchmark_twenty_day_window = _build_window(benchmark_prices, 21)
    ten_day_drawdown = ten_day_window.drawdown or 0.0
    twenty_day_drawdown = twenty_day_window.drawdown or 0.0
    benchmark_twenty_day = benchmark_twenty_day_window.drawdown or 0.0
    relative_underperformance = twenty_day_drawdown - benchmark_twenty_day
    comparison_window = closes[-(threshold.new_low_window + 1) : -1]
    recent_lows = min(comparison_window) if comparison_window else last_close
    new_low = last_close < recent_lows

    reasons = []
    if ten_day_drawdown >= threshold.ten_day_drawdown:
        reasons.append("ten_day_drawdown")
    if twenty_day_drawdown >= threshold.twenty_day_drawdown:
        reasons.append("twenty_day_drawdown")
    if relative_underperformance >= threshold.relative_underperformance:
        reasons.append("relative_underperformance")
    if new_low:
        reasons.append("fresh_low")

    triggered = len(reasons) >= 2
    return TriggerResult(
        triggered=triggered,
        reasons=reasons,
        ten_day_drawdown=round(ten_day_drawdown, 4),
        twenty_day_drawdown=round(twenty_day_drawdown, 4),
        relative_underperformance=round(relative_underperformance, 4),
        new_low=new_low,
        ten_day_window=ten_day_window,
        twenty_day_window=twenty_day_window,
        benchmark_twenty_day_window=benchmark_twenty_day_window,
        fresh_low_window=threshold.new_low_window,
    )


def _build_window(prices: list[PriceBar], lookback_points: int) -> PriceWindow:
    start_bar = prices[-lookback_points]
    end_bar = prices[-1]
    drawdown = None
    if start_bar.close:
        drawdown = round(1 - (end_bar.close / start_bar.close), 4)
    return PriceWindow(
        start_date=start_bar.trading_date,
        end_date=end_bar.trading_date,
        start_close=start_bar.close,
        end_close=end_bar.close,
        drawdown=drawdown,
    )
