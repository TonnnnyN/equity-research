from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from market_sentiment.models import PriceBar, Threshold
from market_sentiment.triggers import compute_trigger


def make_prices(ticker: str, closes: list[float]) -> list[PriceBar]:
    start = date(2026, 1, 1)
    prices = []
    for index, close in enumerate(closes):
        prices.append(
            PriceBar(
                ticker=ticker,
                trading_date=start + timedelta(days=index),
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=1000 + index,
                source="test",
            )
        )
    return prices


class TriggerTests(TestCase):
    def test_trigger_detects_large_relative_drawdown(self) -> None:
        threshold = Threshold(
            ten_day_drawdown=0.10,
            twenty_day_drawdown=0.15,
            relative_underperformance=0.05,
            new_low_window=3,
        )
        prices = make_prices("NVDA", [100, 100, 100, 99, 98, 97, 96, 95, 94, 92, 90, 88, 86, 85, 84, 83, 82, 81, 80, 79, 78])
        benchmark = make_prices("SOXX", [100, 100, 100, 100, 100, 99, 99, 98, 98, 98, 97, 97, 97, 96, 96, 96, 95, 95, 95, 95, 95])

        result = compute_trigger(prices, benchmark, threshold)

        self.assertTrue(result.triggered)
        self.assertIn("ten_day_drawdown", result.reasons)
        self.assertIn("relative_underperformance", result.reasons)
        self.assertEqual(result.ten_day_window.start_date, date(2026, 1, 11))
        self.assertEqual(result.ten_day_window.end_date, date(2026, 1, 21))
        self.assertAlmostEqual(result.ten_day_window.start_close, 90)
        self.assertAlmostEqual(result.ten_day_window.end_close, 78)
        self.assertEqual(result.benchmark_twenty_day_window.start_date, date(2026, 1, 1))
