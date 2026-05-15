---
name: market-sentiment-research
description: Explainable end-of-day pullback research for U.S. stocks. Use when Codex needs to review a stock selloff, run or maintain the market_sentiment daily pipeline, refresh target-pool prices, inspect review packets, or decide Reject, Watch, Starter, Add, or Exit using price action, official disclosures, fundamentals, valuation, positioning, catalysts, and shared runtime resources.
---

# Market Sentiment Research

## Purpose

Use this Skill for single-name or short-list market sentiment research after a meaningful selloff. It answers:

Is this pullback worth rejecting, watching, starting, adding to, or exiting?

This is not the broad U.S. small/mid-cap screening Skill. For prepared-universe candidate generation, use `$us-smallmid-dislocation`.

## Bundle Map

- `defaults/targets.toml`: portable target pool for Skill-level research.
- `defaults/watchlist.toml`: portable watchlist default copied from the local runtime config.
- `scripts/update_price_cache.py`: refresh local target-pool price caches.
- `references/research_workflow.md`: decision workflow, evidence lanes, source priority, event calendar checks.
- `references/target_pool.md`: target universe and target-pool cache conventions.
- `references/script_reference.md`: script-specific usage.
- `docs/design_notes.md`: why this Skill is shaped as pullback research instead of a broad screen.

Shared project resources live outside this Skill:

- `../../sharing-resources/src/market_sentiment/`: Python runtime engine.
- `../../sharing-resources/references/configuration_and_secrets.md`: environment variables and secrets policy.
- `../../sharing-resources/references/data_and_outputs.md`: generated report and review-packet layout.
- `../../sharing-resources/references/runtime_and_shared_scripts.md`: CLI and shared script reference.

## Core Workflow

1. Identify the trigger first: price move, benchmark-relative move, earnings, filing, guidance, macro, rates, commodity, or flow.
2. Run the event calendar precheck for `[T-3, T+5]`.
3. Gather evidence in these lanes: price context, official disclosures, fundamentals and valuation, positioning and flow, catalyst path, bear check.
4. Prefer primary evidence over commentary: SEC filings, IR materials, earnings releases, regulated data, and company disclosures.
5. Decide one action: `Reject`, `Watch`, `Starter`, `Add`, or `Exit`.

Rules:

- `Watch`, `Starter`, and `Add` require explicit `invalidate_if`.
- `Starter` and `Add` require explicit `rerate_if`.
- A cheap multiple without a catalyst is not enough.
- Thin evidence, uncontrolled fresh lows, major scheduled catalysts, weak peer comparison, or low confidence should cap the action at `Watch`.
- Final synthesis stays with the orchestrating agent. Subagents may summarize filings, news, social data, peers, or transcripts, but they should not make the final portfolio action.

## Rule Engine vs LLM Judgement

Review packets contain two layers:

- **Layer 1 (advisory only)**: `bucket_scores`, `rule_engine_precheck`, `decision_summary`. These come from a deterministic Python rule engine. Treat them as a quick sanity check, not as a conclusion.
- **Layer 2 (your evidence base)**: `price_context` (recent ~90 trading days of security and benchmark bars), `fundamentals_snapshot`, `official_events`, `social_summary`, `macro_summary`, `source_health`. These are the raw inputs you must reason over.

Independent scoring is mandatory:

1. Read the Layer 2 raw evidence first. Form your own view of each dimension (fundamentals strength, disclosure tone, peer/market context, price flow including MA-distance and recent lows, risk red flags, social rebound) before looking at Layer 1.
2. Only after your independent assessment, compare with Layer 1. If your conclusion diverges from `rule_engine_precheck.state`, you must state explicitly in the report which Layer 2 evidence drove the divergence.
3. Hard veto remains binding. When `rule_engine_precheck.veto_reason` is non-null (e.g., `negative_official_keyword`, `companyfacts_structural_break`), the action is capped at `Reject` regardless of your other reasoning. These vetos are objective conditions on SEC disclosures or filed financials, not soft signals.
4. `bucket_scores` numeric values (e.g., `social_rebound: 0/10`) are mechanical and frequently noisy when sample sizes are small. Treat them as one observation, not as truth. Re-judge the dimension from the raw `social_summary` (especially `representative_posts`, `recent_stance`, `top_bullish_themes`, `top_bearish_themes`) when their sample is thin.
5. Your output's final action (`Reject` / `Watch` / `Starter` / `Add` / `Exit`) is **your** judgement, not the rule engine's. Quote specific Layer 2 evidence (dates, numbers, post excerpts) when justifying it.

## Runtime Path

For a daily pipeline run from the repository root:

```bash
source sharing-resources/secrets/market_sentiment.secrets.sh
python3 -m pip install -e .
market-sentiment --config config/watchlist.toml preflight
market-sentiment --config config/watchlist.toml run-daily
```

For target-pool price cache refresh:

```bash
python3 skills/market-sentiment-research/scripts/update_price_cache.py
```

Inspect daily outputs under `data/reports/<date>/`, especially `report.md`, `report.json`, `review_queue.md`, `review_packets/`, and `manual_agent_report.zh.md`.

## Output Shape

Lead with:

- conclusion
- action
- decision snapshot

Then provide:

- event calendar
- trigger and attribution
- evidence by lane
- risks and invalidation
- rerate conditions when relevant
- source links

Keep the answer in the user's language unless they ask otherwise.
