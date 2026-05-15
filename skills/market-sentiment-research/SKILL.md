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

## Troubleshooting & Self-Recovery

### When you hit any error, run preflight first

Always start with the mandatory preflight command:

```bash
market-sentiment --config config/watchlist.toml preflight
```

The output is tagged `[OK] / [WARN] / [FAIL]` for each check. Any `[FAIL]` line indicates a real blocker—read its message carefully, as it names the specific env var, file, or config that needs fixing. If a `[FAIL]` points to a missing env var or config file, tell the human user exactly which variable or file to set and what value goes in it. Do not silently retry.

### Python environment baseline

The pipeline requires **Python ≥ 3.11** because `tomllib` is stdlib in 3.11+. On macOS the working interpreter is typically `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12`.

If you see `ModuleNotFoundError: No module named 'tomllib'`, you are on Python 3.10 or older. Switch interpreters or recreate the environment on 3.11+.

If you see `[SSL: CERTIFICATE_VERIFY_FAILED]` from `urlopen` or `requests`, the code already routes through `certifi` (see `http.py` and `tiger.py`). If you still encounter this error, run `python -m pip install --upgrade certifi` in the active interpreter and re-run preflight. Do not disable SSL verification.

### Tiger API setup (most common real-world blocker)

`TIGER_CONFIG_PATH` must point to a directory containing **both** of these files:

- `tiger_openapi_config.properties` (tiger_id, account, license, environment, private_key)
- `tiger_openapi_token.properties` (user token—generated in Tiger developer console, not in code)

If you see `code=2400 user token cannot be empty`, the token file is missing or stale. Ask the human user to regenerate the token in Tiger's developer console and place the new `.properties` file in the same directory. Restart the Python process after the file is in place.

If Tiger returns an empty DataFrame for a `.HK` ticker, the account does not have HK Level 1 market-data subscription. **This is expected behavior**; Yahoo Finance auto-fallback handles it. Do not flag this as a bug; note "price source = yahoo_chart" in your final writeup.

### API keys you will be asked about

- `ALPHAVANTAGE_API_KEY` — required (blocking). Free key from `alphavantage.co`. Quota is 25 calls/day; after that calls silently return empty and the chain falls through to Yahoo Finance.
- `FRED_API_KEY` — required for macro context (DGS10, DFF). Free at `fred.stlouisfed.org`.
- `EIA_API_KEY` — optional. Skip unless commodity context matters.
- `SEC_USER_AGENT` — required. Format: `"YourName email@example.com"`. SEC blocks anonymous traffic.
- `DEEPSEEK_API_KEY` — required for social sentiment judging. Model defaults to `deepseek-v4-flash` with `thinking={type:disabled}` already baked in.
- `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` — **NOT required** for the public `search.json` path the project uses. If preflight WARNs about them, that warning is informational; social posts will still be fetched.

### Price fallback chain — how to read it

Price data follows this order: **Tiger → Yahoo Chart → Alpha Vantage → Stooq → 60-day SQLite cache**.

In any review packet, the `source_health` array names which source actually delivered. Each entry is `{source, success, partial, message}`. A source with `success=False` is not an error—it just means the chain advanced to the next tier. Only worry when **every** entry in the chain failed; then `source_health` will include a `daily_prices_cache` entry with `partial=True` and a message like `using cached prices through <date>; ...`.

Check the top-level `data_quality` field before trusting the bucket scores: `"ok"` means fresh data; `"insufficient"` means you are reading stale or partial data and the rule engine has capped any `ADD`/`STARTER` decision to `Watch`.

### Decision rule — when to self-recover vs ask the user

- If preflight is `[OK]` everywhere but the pipeline produced `data_quality: insufficient`, self-handle: write the report with the cache-fallback caveat in the risks section. No need to interrupt the user.
- If preflight has a `[FAIL]` on an API key or config file, stop and ask the user with the specific env var name, the specific file path, and a one-line link to the provider's signup page. Do not retry until they confirm.
- If a Tiger call raises an exception not covered above, capture the traceback tail, run preflight to confirm credentials are still valid, then ask the user—quote the exception and the preflight output. Do not attempt to patch SDK behavior at runtime.

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

Inspect daily outputs under `data/reports/<date>/`:
- `manual_agent_report.zh.md` — the single human-facing narrative covering all triggered tickers.
- `review_packets/<TICKER>.json` — machine-readable structured evidence for your independent reasoning per ticker.

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
