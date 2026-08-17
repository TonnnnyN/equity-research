# Data And Outputs

## Generated Data

The `data/` folder is generated runtime state. It can be large and should not be treated as source material for the skill unless the task is to inspect a historical run.

Do not commit `data/`. Some raw provider payloads and rerun artifacts can contain request metadata or query-parameter fields from upstream APIs.

Main folders:

- `data/raw/`: raw provider payloads by run date
- `data/reports/`: generated daily reports and review packets
- `data/state/`: SQLite state, cookies, provider DBs, and other local state
- `data/reruns/`: manual rerun artifacts

## Daily Report Folder

Expected shape:

```text
data/reports/YYYY-MM-DD/
  review_packets/
  manual_agent_report.zh.md
```

## How To Use Outputs

For quick inspection, start with `manual_agent_report.zh.md` when it exists. It is designed for human and Agent review in Chinese.

For structured processing, use `review_packets/*.json`. Each packet's top-level `valuation_inputs`
block (schema_version 1.3+) holds SEC-only valuation data-layer evidence: `fundamentals` (up to 20
quarters of history per concept, each datapoint keeping `end`/`filed`/`form` for restatement-aware
reads, plus a `derived` boolean and — when `derived` is true — a `derived_from` trace showing which
two cumulative facts were subtracted to recover a quarter the filer never reported standalone; a
`derived: false` datapoint is one the filer actually reported) and `derived` (TTM sums, total liquid
assets, `non_operating_assets` — illiquid strategic/venture equity stakes such as
`LongTermInvestments`, reported separately from `total_liquid_assets` and never counted as cash-like
— net cash, enterprise value, market cap, beta, and a `data_gaps` list naming every missing required
input). Every derived field states which raw inputs and source produced it; this block is Layer 2
advisory evidence only and never affects `bucket_scores` or `partial_coverage` — see
`docs/architecture.md` for the extraction and computation design.

For audit trail or source debugging, inspect `data/raw/<date>/` and storage records.

## Analyst Targets — Yahoo Crumb Flow And History

`sources/analyst_targets.py` authenticates to Yahoo's `quoteSummary` endpoint through a private
cookie+crumb handshake (`fc.yahoo.com` for a session cookie, then
`query1.finance.yahoo.com/v1/test/getcrumb` for the crumb). This is an undocumented, reverse-engineered
endpoint, not a published API, and can break without notice; every failure path degrades gracefully —
to Finnhub when `FINNHUB_API_KEY` is set, otherwise to a clean no-op — and never blocks the pipeline.

On every successful fetch (Yahoo or Finnhub) it persists two SQLite tables in `data/state/`:
`analyst_consensus_snapshots` (one row per `ticker`/`run_date`, `INSERT OR REPLACE` so a rerun
overwrites same-day data) and `analyst_rating_actions` (one row per firm rating action, deduped via
`INSERT OR IGNORE` on `ticker`/`firm`/`action_date`/`to_grade`). Yahoo's `upgradeDowngradeHistory` is
parsed in full rather than capped, so a ticker's first-ever fetch backfills its entire available rating
history in one call.

From that stored history, `_compute_history_signals` derives change-over-time fields inside the
existing `analyst_summary.history` block of the review packet: `days_since_last_snapshot`,
`days_since_target_changed`, `target_change_pct` / `price_change_pct` (since the previous snapshot, and
since 30d/90d where a snapshot exists at or before that cutoff), `dispersion` (`(target_high -
target_low) / target_mean`, with a plain-language note once analysts disagree by 30% or more of the
target mean), `recent_actions_90d` (up/down/init counts and firm names), and `lead_lag` — a conservative
classification of whether the target moved before (`analyst_led`) or after (`analyst_followed`) the
price, anchored on the most recent up/down rating action and a ±10-day / 3%-price-move window; anything
not clearly distinguishable is reported as `unclear` rather than guessed. As with `valuation_inputs`,
this is Layer 2 advisory evidence only: it never enters `bucket_scores`, never sets `partial_coverage`,
and every field carries a stated reason (in `history.reasons`) instead of a value when stored history is
too thin to support it.

**Calibration (Zoom, live fetch 2026-08-17):** the fetch returned `source: yahoo_quote_summary` (no
silent Finnhub fallback). A first-ever fetch backfilled 387 rating-action rows spanning 2019→2026 across
roughly 44 firms — but only about 35 of those are real up/down actions; the rest are maintains,
reiterations, and initiations (`action` values `main`/`reit`/`init`). `dispersion` came out at 0.487
(target high 135 / low 79 / mean 115) — analysts disagreeing by roughly half the share price. The most
recent genuine up/downgrade was Keybanc on 2026-05-22, nearly three months before the run date. On a
fresh database, `lead_lag` correctly returned `unclear` with reason `insufficient price history`, and
both `days_since_*` fields correctly returned `None` with a stated reason.

This sparsity is typical, not a Zoom-specific artifact: a handful of real rating changes per name per
year, wide dispersion, and long gaps between actions. That is why this lane stays advisory context and
is never wired into scoring.

## Retention

Retention is configured in `config/watchlist.toml` under `[retention]`.

The cleanup command is:

```bash
market-sentiment --config config/watchlist.toml cleanup-data --date YYYY-MM-DD
```

Cleanup should preserve the reference date's current report and avoid deleting the latest output unexpectedly.
