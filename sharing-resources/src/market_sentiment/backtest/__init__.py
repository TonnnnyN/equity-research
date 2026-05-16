"""Backtest subsystem for the market-sentiment rule engine.

Pipeline: build_backtest_dataset -> status_engine -> simulator -> runner.
US equities only; social and options lanes are intentionally omitted because
they cannot be reconstructed point-in-time.
"""
