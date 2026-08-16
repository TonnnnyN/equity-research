# Dislocation Workflow

## Goal

Find U.S. small/mid-cap candidates where price action appears disconnected from recent business evidence.

This workflow runs three stages and produces screening states only:

- `Pass`
- `Watchlist`
- `Investigate`

It does not produce `Reject`, `Starter`, `Add`, or other portfolio actions.

---

## Stage 0: Universe Construction

**Script**: `scripts/build_universe.py`

Build a prepared CSV universe before running the screen. The deterministic screen (`screen_candidates.py`) consumes a static CSV; it does not fetch market data itself.

`build_universe.py` assembles the CSV in four sequential phases to minimize API calls:

1. Fetch the SEC `company_tickers_exchange.json` seed list; keep only `allowed_exchanges`.
2. Fetch Yahoo Finance daily bars; apply the `min_price_usd` and `min_avg_dollar_volume_20d_usd` filters.
3. Compute `market_cap_usd`; apply the `[market_cap_min_usd, market_cap_max_usd]` range.
4. For the ~2,200–3,200 surviving tickers, call SEC `companyfacts` to fill fundamental columns.

Per-ticker results are cached locally; reruns resume from where they left off.

Output: `data/universe/universe_<date>.csv` (columns match `references/input_schema.md`).

See `references/script_reference.md` for the full command and flags.

---

## Stage 1: Deterministic Screen

**Script**: `scripts/screen_candidates.py`

### Universe First

Before scoring a candidate, remove names with unsuitable structure or weak tradability:

- market cap outside the configured range
- weak average dollar volume
- OTC, SPAC shell, closed-end fund, ETF, ADR when excluded by config
- stale filings or missing core fields
- hard special situations that distort price action

The universe size is a soft target, not a requirement.

### Trigger Logic

Require both:

- a primary dislocation family, such as large drawdown, fresh lows, high downside from moving averages, or strong benchmark-relative underperformance
- a confirming family, such as good earnings with bad tape, stable fundamentals, revision resilience, or sector-adjusted disconnect

Use `references/thresholds.md` for exact threshold guidance.

### Red-Flag Order

Apply red flags before ranking:

1. Hard red flags usually force `Pass`.
2. Strong negative signals usually cap at `Watchlist`.
3. Structural distortions require explicit notes.
4. Value-trap patterns reduce priority unless the candidate has a concrete near-term repair path.

Use `references/red_flags.md` for detailed examples.

Output: `smallmid_results.json` with `Pass / Watchlist / Investigate` states, and `smallmid_report.md`.

---

## Stage 2: Subagent Deep Review

**Triggered by**: non-Pass entries in `smallmid_results.json`

After Stage 1, the main agent reads `smallmid_results.json` and dispatches one Sonnet subagent per non-Pass candidate using parallel tool calls (batches of 6–8). Each subagent:

1. Runs `market-sentiment review-ticker <T>` to produce a review packet.
2. Reads Layer 2 evidence first (price_context, fundamentals_snapshot, official_events, social_summary, macro_summary, source_health) and forms an independent view before consulting Layer 1 (bucket_scores, rule_engine_precheck).
3. Returns a compact JSON verdict with `refined_state`, `conviction`, `key_evidence`, `kill_risks`, `next_checks`, and divergence information.

Subagents do not produce the final combined ranking. The main agent aggregates all verdicts into the final candidate report.

See `references/subagent_workflow.md` for the exact verdict schema, field definitions, and the Layer-2-first scoring protocol.

---

## Escalation

`Investigate` (whether assigned by Stage 1 or confirmed/upgraded by Stage 2 subagents) means the name deserves deeper research. The next step should verify:

- latest filing and balance-sheet risk
- exact event that caused the move
- whether guidance, revisions, or backlog actually remain stable
- liquidity and tradability
- upcoming catalysts
- peer-relative valuation and quality
