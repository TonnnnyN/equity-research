# Script Detailed Reference

## `scripts/update_price_cache.py`

Purpose: refresh a local target-pool price cache from Yahoo Chart.

Use when:

- local target-pool prices are stale
- a stock review should use local cached prices before public web lookup
- the user asks to refresh the market sentiment target pool

Command:

```bash
python3 scripts/update_price_cache.py
```

Path resolution:

1. `MARKET_SENTIMENT_TARGET_POOL`
2. `~/market_sentiment_target_pool`
3. `/Users/votee_tommy/市场情绪目标池`

Expected target-pool input:

```text
targets.toml
```

Default target-pool reference:

```text
references/target_pool.md
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

## `scripts/screen_candidates.py`

Purpose: screen a prepared U.S. small/mid-cap CSV for severe price dislocation candidates with basic quality filters.

Use when:

- the user provides a prepared CSV universe
- the task is broad candidate triage, not a single-stock buy memo
- the desired output is `Pass`, `Watchlist`, or `Investigate`

Command:

```bash
python3 scripts/screen_candidates.py input.csv \
  --output-json output/smallmid_results.json \
  --output-markdown output/smallmid_report.md
```

Default config:

```text
defaults/universe.toml
```

Read before use:

- `references/smallmid_input_schema.md`
- `references/smallmid_thresholds.md`
- `references/smallmid_red_flags.md`

Important behavior:

- Filters out excluded structures, sectors, weak liquidity, and out-of-range market caps.
- Requires both a primary dislocation signal and a confirming signal.
- Caps or rejects candidates with hard red flags.
- Produces ranked outputs, not final portfolio actions.

## CLI Entry Point

The Python package exposes:

```bash
market-sentiment
```

Core commands:

```bash
market-sentiment --config config/watchlist.toml init-db
market-sentiment --config config/watchlist.toml preflight
market-sentiment --config config/watchlist.toml run-daily
market-sentiment --config config/watchlist.toml show-report --date YYYY-MM-DD
market-sentiment --config config/watchlist.toml cleanup-data --date YYYY-MM-DD
```

Use `preflight` before diagnosing provider failures. It distinguishes missing API keys, X account/cookie setup, options configuration, and optional provider coverage.

## `scripts/redact_sensitive_json.py`

Purpose: remove sensitive values from generated JSON files before archiving, sharing, or debugging.

Command:

```bash
python3 scripts/redact_sensitive_json.py data
```

Dry run:

```bash
python3 scripts/redact_sensitive_json.py --dry-run data
```

Redacted keys include `api_key`, `apikey`, `token`, `secret`, `client_secret`, `password`, `email_password`, and `authorization`. URL query strings with those keys are also rewritten.
