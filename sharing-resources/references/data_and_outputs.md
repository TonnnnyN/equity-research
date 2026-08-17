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

## Review Packet Tiers

Review packets are saved in three tiers. Read them in order of size and specificity:

### Tier 1 — Delta Packet (~1–3 KB)
**File:** `<ticker>.delta.json` (optional; only created if there is a prior run)

Contains what changed since the last derivation:
- `days_since_previous`: days elapsed since the last full derivation
- `price_move_pct`: percentage price change since then
- `what_changed`: boolean flags for fundamentals change, new filing, analyst action, trigger state change
- `current_state`: today's trigger state and score
- `previous_judgement`: the prior portrait and order (JSON strings)

**Critical instruction in the delta packet:** `Do NOT read this field until you have formed and recorded your own assessment.` Reading the prior conclusion before deriving your own makes a fresh analysis indistinguishable from a copy.

When to use: for maintenance checks, when re-confirming a prior judgement. The delta answers "what's different?" — not "is my previous judgement still valid?" (that's a full re-derivation).

### Tier 2 — Working Packet (~31 KB)
**File:** `<ticker>.json` (always created; this is the default for agents to load)

Compressed version of the full packet:
- `price_context` contains only `latest_*` and `price_summary` (compact metrics: MA distances, recent moves, range position) instead of 180 individual bars
- `valuation_inputs.fundamentals` is replaced with a `fundamentals_note` pointing to the full packet
- `social_summary` is dropped (agent can fetch social URLs via the `social_sources_to_fetch` list if needed)
- All other fields intact

**When to use:** for decision-making. This is the standard input to the agent workflow. Agents load this by default when running `equity-research review TICKER`.

### Tier 3 — Full Packet (~290 KB)
**File:** `<ticker>.full.json` (archive; never loaded by default)

Complete uncompressed packet with all bars, all fundamentals history, all social summaries. The working packet carries a `full_packet_path` field pointing to this.

**When to use:** for audit, restatement-aware fundamental reconciliation, or deep historical analysis. Load this only if the Tier 2 working packet's evidence summary is incomplete or you need to verify the raw bar-by-bar price history.

## Specification Metadata

Every review packet includes a `specification_metadata` block at the top level. This records whether the caller specified a layer and a benchmark, and which defaults were applied:

```json
"specification_metadata": {
  "layer_was_specified": false,
  "benchmark_was_specified": false,
  "layer_note": "Layer was not specified; generic thresholds were used for trigger calculations.",
  "benchmark_note": "Benchmark was not specified; QQQ was used as default for relative performance calculations."
}
```

**Why it exists:** An unspecified layer means generic (one-size-fits-all) thresholds and a skipped peer comparison. An unspecified benchmark means QQQ was used for relative-strength calculations. The reader needs to know these defaults were applied — relative-strength numbers measured against a benchmark nobody explicitly chose are less reliable than numbers measured against a chosen peer set. This field makes the assumptions visible.

## Rederivation Required

Every review packet includes a `rederivation_required` block indicating whether a cheap maintenance check is sufficient or if a full re-derivation is necessary:

```json
"rederivation_required": {
  "required": true,
  "reasons": ["new 10-Q filed on 2026-08-15", "price moved +24.5% since derivation (threshold: 20%)"],
  "checks": {
    "new_filings_since_derivation": {"checked": true, "found": true, "count": 1},
    "days_since_derivation": {"checked": true, "days": 18, "threshold_days": 30, "exceeded": false},
    "price_movement_since_derivation": {"checked": true, "pct_change": 24.5, "exceeded": true},
    "trigger_conditions_active": {"checked": true, "any_active": false}
  }
}
```

The check fires if any of these are true:
1. A new 10-K or 10-Q was filed since the last full derivation
2. More than 30 days have passed since the last full derivation
3. Price has moved more than 20% (cumulative) since the last derivation
4. Any trigger condition is currently active (drawdown, underperformance, fresh low)

**Critical distinction:** `rederivation_required.required` measures time since the last **full derivation**, not since the last run. Skipping days does not by itself force a full pass. A cheap update is sufficient when `required: false` — that means confirming that a prior judgement still holds. This is **not** an independent re-derivation and must never be reported as one.

## How To Use Outputs

For quick inspection, start with `manual_agent_report.zh.md` when it exists. It is designed for human and Agent review in Chinese.

For structured processing, use `review_packets/*.json`. 

**Load order:**
1. If `rederivation_required.required` is `false`, read only the Tier 1 delta packet to confirm the prior judgement still holds.
2. If `rederivation_required.required` is `true`, or if this is the first run (no prior judgement), load the Tier 2 working packet (`<ticker>.json`) and perform a full re-derivation.
3. Only load the Tier 3 full packet (`<ticker>.full.json`) if you need to verify raw data or reconcile restatements.

Each packet's top-level `valuation_inputs` block (schema_version 1.3+) holds SEC-only valuation data-layer evidence: `fundamentals` (up to 20
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

Retention is configured in `config/default.toml` under `[retention]`.

The cleanup command is:

```bash
equity-research --config config/default.toml cleanup-data --date YYYY-MM-DD
```

Cleanup should preserve the reference date's current report and avoid deleting the latest output unexpectedly.
