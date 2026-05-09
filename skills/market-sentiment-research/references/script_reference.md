# Script Reference

## `scripts/update_price_cache.py`

Purpose: refresh a local target-pool price cache from Yahoo Chart.

Use when:

- local target-pool prices are stale
- a stock review should use local cached prices before a live lookup
- the target pool needs a deterministic refresh without API keys

Command from the repository root:

```bash
python3 skills/market-sentiment-research/scripts/update_price_cache.py
```

Path resolution:

1. `MARKET_SENTIMENT_TARGET_POOL`
2. `~/market_sentiment_target_pool`

Expected target-pool input:

```text
targets.toml
```

Expected outputs:

```text
prices/daily/<TICKER>.csv
prices/current/<TICKER>.json
prices/manifest.json
```

Notes:

- The script uses only the Python standard library.
- It does not require project API keys.
- If the target-pool path is missing, set `MARKET_SENTIMENT_TARGET_POOL`.
