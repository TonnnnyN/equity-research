# Target Pool

## Default Universe

The portable target-pool snapshot lives at:

```text
skills/market-sentiment-research/defaults/targets.toml
```

The runtime watchlist lives at:

```text
config/watchlist.toml
```

Use the Skill default when the task is an Agent Skill research task and no live runtime config is needed. Use `config/watchlist.toml` when running the actual CLI pipeline.

## Default Benchmarks

- `QQQ`: AI applications
- `SOXX`: compute and semis
- `XLU`: utilities and power
- `PPH`: large-cap pharma giants
- `3033.HK`: HK-listed China robotics leaders
- `SPY`: broad market reference

## Runtime Compatibility Notes

Large-cap pharma and HK robotics are represented under the `ai_applications` runtime layer in the current CLI config so the existing rule engine can process them without a code change.

Their benchmark fields still point to their own better sector references:

- pharma: `PPH`
- HK robotics: `3033.HK`

## Local Price Cache

The price-cache refresh helper is:

```bash
python3 skills/market-sentiment-research/scripts/update_price_cache.py
```

It resolves the target-pool directory in this order:

1. `MARKET_SENTIMENT_TARGET_POOL`
2. `~/market_sentiment_target_pool`

Expected cache shape:

```text
prices/daily/<TICKER>.csv
prices/current/<TICKER>.json
prices/manifest.json
```
