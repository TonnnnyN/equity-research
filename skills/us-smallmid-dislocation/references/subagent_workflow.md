# Subagent Workflow

Full three-stage data flow for the `us-smallmid-dislocation` Skill, and the exact schema for Stage 2 subagent verdicts.

---

## Stage 0: Universe Construction

**Script**: `scripts/build_universe.py`

**Inputs**

- Seed ticker list from SEC `company_tickers_exchange.json` (default) or a custom `--seed-file`.
- Configuration from `defaults/universe.toml`: allowed exchanges, market-cap bounds, price floor, ADV floor.
- Yahoo Finance daily bars (price, volume) via `YahooFinanceClient`.
- SEC `companyfacts` JSON via `SecClient.fetch_company_facts`.

**Processing order** (staged to minimize API calls)

1. Fetch seed list; keep only `allowed_exchanges`.
2. Fetch Yahoo daily bars; apply `min_price_usd` and `min_avg_dollar_volume_20d_usd` filters.
3. Compute `market_cap_usd = price × shares_outstanding`; apply `[market_cap_min_usd, market_cap_max_usd]` filter.
4. For surviving tickers (target ~2,200–3,200), call SEC `companyfacts` to fill fundamental columns.

**Output**

- `data/universe/universe_<date>.csv` (columns match `references/input_schema.md`).
- Columns that cannot be reliably sourced (e.g. `runway_months`, `sector`, `flags`) are left empty; `screen_candidates.py` is None-safe.
- End-of-run summary: `Succeeded N / Skipped M / Top-5 skip reasons`.

**Caching**: per-ticker results are cached locally under `data/state/universe_cache/`. Reruns skip already-fetched tickers.

---

## Stage 1: Deterministic Screen

**Script**: `scripts/screen_candidates.py`

**Input**: `universe_<date>.csv`

**Processing**

1. Universe hygiene filters (exchange, structure, market cap, liquidity, filing freshness).
2. Hard red-flag evaluation (forces `Pass` for bankruptcy, going-concern, fraud, delisting, restatement).
3. Primary + confirming dislocation signal check (thresholds from `references/thresholds.md`).
4. State ceiling application from `[state_ceilings]` in `universe.toml`.
5. Scoring and ranking.

**Output**

- `smallmid_results.json`: list of objects, each containing at minimum `ticker`, `state` (`Pass` / `Watchlist` / `Investigate`), and the scored fields.
- `smallmid_report.md`: human-readable ranked summary.

---

## Stage 2: Concurrent Subagent Deep Review

This stage applies to every entry in `smallmid_results.json` where `state != "Pass"`.

### Orchestration

1. The main agent reads `smallmid_results.json` and collects all non-Pass candidates.
2. For each candidate, the main agent dispatches **one Sonnet subagent** using parallel Task tool calls, in **batches of 6–8** per message. Each batch must complete before the next is sent.
3. Each subagent runs independently: it fetches the review packet, applies Layer-2-first independent scoring, and returns exactly one verdict JSON object.
4. The main agent waits for all batches, collects verdicts, and builds the final ranking report.

### Subagent task

For ticker `<T>` with `screen_state` from Stage 1:

```
Step 1: market-sentiment review-ticker <T>
         → writes data/reports/<date>/review_packets/<T>.json
         → also prints packet to stdout

Step 2: Read Layer 2 evidence first:
         price_context, fundamentals_snapshot, official_events,
         social_summary, macro_summary, source_health
        Form an independent view before reading Layer 1:
         bucket_scores, rule_engine_precheck, decision_summary

Step 3: Return the compact JSON verdict below and nothing else.
```

### Verdict JSON schema

```json
{
  "ticker": "ABCD",
  "screen_state": "Investigate",
  "refined_state": "Investigate",
  "conviction": "high",
  "key_evidence": [
    "Revenue +12% YoY per latest 10-Q filed 2026-04-15",
    "Price -38% from 52w high vs IWM -6%; no macro or sector explanation"
  ],
  "kill_risks": [
    "Convertible note maturity in 90 days; refinancing not confirmed"
  ],
  "next_checks": [
    "Verify convertible note terms and cash position in 10-Q",
    "Check for insider selling in latest Form 4s"
  ],
  "diverged_from_screen": false,
  "divergence_reason": null
}
```

**Field definitions**

| Field | Type | Notes |
|---|---|---|
| `ticker` | string | Ticker symbol, uppercase. |
| `screen_state` | string | State assigned by Stage 1: `Pass`, `Watchlist`, or `Investigate`. Copy from `smallmid_results.json`. |
| `refined_state` | string | Subagent's verdict: `Pass`, `Watchlist`, or `Investigate` only. |
| `conviction` | string | `high`, `medium`, or `low`. Reflects the subagent's confidence in `refined_state` given available evidence quality. |
| `key_evidence` | array of strings | 2–5 concise items drawn from Layer 2 raw evidence. Each item must cite a specific fact (date, number, or filing). |
| `kill_risks` | array of strings | 1–3 specific conditions that would cause the subagent to downgrade to `Pass`. |
| `next_checks` | array of strings | 1–3 concrete follow-up actions for deeper research. |
| `diverged_from_screen` | boolean | `true` if `refined_state != screen_state`. |
| `divergence_reason` | string or null | Required when `diverged_from_screen` is `true`. Must identify the specific Layer 2 evidence that drove the change. `null` when not diverged. |

**Constraints on `refined_state`**

- Subagents may move a candidate in either direction: upgrade from `Watchlist` to `Investigate`, or downgrade from `Investigate` to `Watchlist` or `Pass`.
- Any change requires `diverged_from_screen: true` and a populated `divergence_reason`.
- Subagents may NOT produce portfolio actions (`Starter`, `Add`, `Exit`). Output vocabulary is limited to `Pass / Watchlist / Investigate`.

### Layer 2-first independent-scoring protocol

Subagents apply the same discipline required by `market-sentiment-research`:

1. Read Layer 2 raw evidence first. Form an independent view of fundamentals strength, disclosure tone, price flow (MA distance, recent lows, benchmark-relative), red-flag risk, and social rebound before reading Layer 1.
2. After independent assessment, compare with Layer 1 (`rule_engine_precheck.state`). If your conclusion diverges, `divergence_reason` must name the specific Layer 2 data point.
3. Hard vetos (`rule_engine_precheck.veto_reason` non-null) are binding. When a veto is present, `refined_state` must be `Pass` regardless of other evidence.
4. `bucket_scores` are mechanical and may be noisy with small sample sizes. Re-judge from raw `social_summary` fields when sample is thin.

### Main agent aggregation

After all subagent verdicts are collected:

- Sort by `refined_state` priority (`Investigate` first, then `Watchlist`) and `conviction` (`high` before `medium` before `low`).
- Flag any ticker where `diverged_from_screen: true` with an explicit divergence note in the report.
- Final output is a ranked candidate table plus per-ticker next-check lists, not a portfolio action.
