# Script Reference

## `scripts/build_universe.py`

Purpose: assemble a universe CSV from public data sources (SEC and Yahoo Finance) that `screen_candidates.py` can consume directly.

Use when:

- no prepared CSV is available
- the task requires an up-to-date universe rather than a manually curated file

Command from the repository root:

```bash
# Full universe run (production):
python3 skills/us-smallmid-dislocation/scripts/build_universe.py \
  --output data/universe/universe_<date>.csv

# Development smoke-test (process only the first 20 tickers):
python3 skills/us-smallmid-dislocation/scripts/build_universe.py \
  --limit 20 \
  --output data/universe/universe_test.csv

# Custom seed file (one ticker per line):
python3 skills/us-smallmid-dislocation/scripts/build_universe.py \
  --seed-file path/to/tickers.txt \
  --output data/universe/universe_custom.csv

# Backdate the drawdown calculation:
python3 skills/us-smallmid-dislocation/scripts/build_universe.py \
  --as-of 2026-04-30 \
  --output data/universe/universe_2026-04-30.csv
```

Arguments:

| Argument | Default | Description |
|---|---|---|
| `--limit N` | none (all) | Process only the first N seed tickers. Use for development and smoke-testing. |
| `--seed-file PATH` | SEC `company_tickers_exchange.json` | Path to a plain-text file with one ticker per line. Omit to use the SEC universe automatically. |
| `--output PATH` | `data/universe/universe_<date>.csv` | Destination CSV path. |
| `--as-of DATE` | today | Reference date (`YYYY-MM-DD`) for drawdown and relative-underperformance calculations. |

Default config:

```text
skills/us-smallmid-dislocation/defaults/universe.toml
```

Important behavior:

- Applies a staged filter pipeline (exchange → price/ADV → market cap → fundamentals) to minimize SEC API calls.
- Per-ticker results are cached under `data/state/universe_cache/`. Reruns skip already-fetched tickers.
- Single-ticker failures are skipped gracefully with a warning; the run never aborts on one bad ticker.
- At the end, prints a summary: `Succeeded N / Skipped M / Top-5 skip reasons`.
- SEC requests are rate-limited to 10 req/s. Yahoo Finance is fetched sequentially; no Alpha Vantage calls are made during universe construction.
- Columns that cannot be sourced reliably (e.g. `runway_months`, `sector`, `flags`) are left empty. `screen_candidates.py` is None-safe for all optional columns.

Read before use:

- `references/input_schema.md` (to understand the output CSV structure)

---

## `scripts/screen_candidates.py`

Purpose: screen a prepared U.S. small/mid-cap CSV for severe price dislocation candidates with basic quality filters.

Use when:

- a prepared CSV universe is available (from `build_universe.py` or manually)
- the task is broad candidate triage, not a single-stock buy memo
- the desired output is `Pass`, `Watchlist`, or `Investigate`

Command from the repository root:

```bash
python3 skills/us-smallmid-dislocation/scripts/screen_candidates.py input.csv \
  --output-json output/smallmid_results.json \
  --output-markdown output/smallmid_report.md
```

Default config:

```text
skills/us-smallmid-dislocation/defaults/universe.toml
```

Read before use:

- `references/input_schema.md`
- `references/thresholds.md`
- `references/red_flags.md`

Important behavior:

- Filters excluded structures, sectors, weak liquidity, and out-of-range market caps.
- Requires both a primary dislocation signal and a confirming signal.
- Caps or rejects candidates with hard red flags.
- Produces ranked outputs, not final portfolio actions.

---

## `market-sentiment review-ticker` (Stage 2 subcommand)

Purpose: run the market-sentiment single-ticker review pipeline on any ad-hoc ticker (not required to be in `watchlist.toml`) and produce a dual-layer review packet.

Used by Stage 2 subagents during concurrent deep review. Can also be run manually for spot-checking a candidate.

Command from the repository root:

```bash
# Basic usage (benchmark defaults to IWM):
market-sentiment review-ticker ABCD

# With explicit options:
market-sentiment review-ticker ABCD \
  --benchmark IWM \
  --date 2026-05-17 \
  --name "Acme Corp"
```

Arguments:

| Argument | Default | Description |
|---|---|---|
| `TICKER` | (required) | Ticker symbol to review. Does not need to be in `watchlist.toml`. |
| `--benchmark` | `IWM` | Benchmark ticker for relative-underperformance calculation. Use `IWM` for small-cap, `IJH` for mid-cap. |
| `--layer` | (auto) | Layer name for threshold lookup. Omit to use the default layer. |
| `--date DATE` | today | Run date (`YYYY-MM-DD`) for the review. |
| `--name TEXT` | (auto) | Display name override for the ticker. |

Output:

- JSON review packet printed to stdout (subagents capture this directly).
- Packet also written to `data/reports/<date>/review_packets/<TICKER>.json`.
- Packet contains Layer 1 (bucket_scores, rule_engine_precheck, decision_summary) and Layer 2 (price_context, fundamentals_snapshot, official_events, social_summary, macro_summary, source_health).

Important behavior:

- Even if `triggered=False`, the full review packet is still produced. Stage 2 uses this packet for evidence review regardless of trigger status, because Stage 1 has already confirmed a dislocation signal.
- Follows the same price fallback chain as the daily pipeline: Tiger → Yahoo Chart → Alpha Vantage → Stooq → SQLite cache.
- Errors in individual data lanes are handled gracefully; they appear in `source_health` as `success=False` without aborting the packet.
