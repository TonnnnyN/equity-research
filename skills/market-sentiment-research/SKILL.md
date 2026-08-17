---
name: market-sentiment-research
description: Explainable end-of-day pullback research for U.S. stocks. Use when Codex needs to review a stock selloff, run or maintain the market_sentiment daily pipeline, refresh target-pool prices, inspect review packets, or decide Reject, Watch, Starter, Add, or Exit using price action, official disclosures, fundamentals, valuation, positioning, catalysts, and shared runtime resources.
---

# Market Sentiment Research

## Purpose

Use this Skill for single-name or short-list market sentiment research after a meaningful selloff. It answers:

Is this pullback worth rejecting, watching, starting, adding to, or exiting?

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
- **Layer 2 (your evidence base)**: `price_context` (recent ~90 trading days of security and benchmark bars), `fundamentals_snapshot`, `official_events`, `social_summary`, `macro_summary`, `analyst_summary` (analyst consensus target price + recent upgrade/downgrade history), `source_health`. These are the raw inputs you must reason over.

**Using `analyst_summary`**: This field is advisory commentary, not primary evidence. Per source priority it sits below SEC filings and fundamentals. Treat `implied_upside` (consensus target vs current price) as a context check on valuation — not a buy/sell signal. Treat `recent_changes` (upgrade/downgrade momentum from `firm`, `date`, `action`, `from_grade`, `to_grade`) as a soft confirmation or divergence signal for the catalyst path. Analyst targets are lagging and subject to herding; they must never override a primary disclosure and must not by themselves move an action up to `Starter` or `Add`.

**Web-search fallback when `analyst_summary` is missing or empty**: Both Yahoo and Finnhub may fail for non-US or `.HK` tickers, leaving the field absent or with null values. In that case, do a WebSearch for the ticker's analyst consensus (e.g. query `"<TICKER> analyst price target consensus"`), checking reputable aggregators such as TipRanks, MarketBeat, or Yahoo Finance. Summarize consensus target and recent rating actions in the report's evidence section, and clearly label it as web-sourced (lower confidence, not reproducible from the pipeline) rather than pipeline data. This is a fallback path, not the main evidence lane.

Independent scoring is mandatory:

1. Read the Layer 2 raw evidence first. Form your own view of each dimension (fundamentals strength, disclosure tone, peer/market context, price flow including MA-distance and recent lows, risk red flags, social rebound) before looking at Layer 1.
2. Only after your independent assessment, compare with Layer 1. If your conclusion diverges from `rule_engine_precheck.state`, you must state explicitly in the report which Layer 2 evidence drove the divergence.
3. Hard veto remains binding. When `rule_engine_precheck.veto_reason` is non-null (e.g., `negative_official_keyword`, `companyfacts_structural_break`), the action is capped at `Reject` regardless of your other reasoning. These vetos are objective conditions on SEC disclosures or filed financials, not soft signals.
4. `bucket_scores` numeric values (e.g., `social_rebound: 0/10`) are mechanical and frequently noisy when sample sizes are small. Treat them as one observation, not as truth. Re-judge the dimension from the raw `social_summary` (especially `representative_posts`, `recent_stance`, `top_bullish_themes`, `top_bearish_themes`) when their sample is thin.
5. Your output's final action (`Reject` / `Watch` / `Starter` / `Add` / `Exit`) is **your** judgement, not the rule engine's. Quote specific Layer 2 evidence (dates, numbers, post excerpts) when justifying it.

## Valuation Layer: A Gate, Not a Second Opinion

`review_packets.json`'s `valuation_inputs.models` (populated when `valuation_inputs.derived` is available) is the output of a 12-model valuation layer built on top of the existing `valuation_inputs.fundamentals` / `valuation_inputs.derived` Layer 2 evidence. It is Layer 2 advisory evidence, exactly like `analyst_summary`: it never enters `bucket_scores`, never sets `partial_coverage`, and can never raise or fail the pipeline. When the router could not build `ValuationDerived` at all, `valuation_inputs.models` is `null` — treat that exactly like a missing lane, not like a "neutral" reading.

**The two systems answer different questions and combine as a constraint, not as a second opinion.** The 110-point rule engine (`bucket_scores`, `rule_engine_precheck`) answers *"is this the moment to act?"* — stabilisation, red flags, sentiment turn, sector-vs-idiosyncratic. The valuation layer answers *"at this price, is acting worth it?"* Neither replaces the other; read both and combine them with this table:

| scoring | valuation | action |
|---|---|---|
| passes | cheap | act; margin of safety sizes the position |
| passes | expensive | **downgrade** (`Add` → `Starter`/`Watch`) |
| fails | cheap | stay at `Watch`, record the valuation anchor |
| fails | expensive | `Reject` |

**Rule: the valuation gate is one-directional.** Valuation may only *lower* an action, never raise one. A cheap multiple must never lift `Watch` to `Add`. The scoring engine's inputs are objective disclosures and price behaviour; valuation rests on assumptions (a discount rate, a growth path, a peer set). An assumption-driven model must never be allowed to override an evidence-driven veto — a hard veto from `rule_engine_precheck.veto_reason` or a scoring-engine `Reject`/`Watch` stays capped regardless of how cheap the stock looks.

### How to read `valuation_inputs.models`, in order

1. **`router.enabled_models` and `router.excluded`.** The excluded list, with its per-model reason, is equally important as what ran — it tells you which lenses are structurally unavailable for this name (e.g., DCF variants excluded outright when `ttm_fcf` is negative) versus merely skipped for missing data on this run.
2. **`reverse_dcf` as the primary lens.** It does not produce a fair-value target. It solves, from the CURRENT enterprise value, for the FCF growth rate the market is implicitly pricing in at each discount rate in `router.profile.discount_rates`. Your job is to judge whether that implied growth is pessimistic or optimistic given the Layer 2 evidence you already gathered (fundamentals trend, disclosure tone, catalyst path) — not to produce your own price target.
3. **The downside models** (`net_cash_floor`, `sum_of_the_parts`, `cash_runway`) — where is the floor, and how much of the current price is covered by liquid assets alone.
4. **Quality and risk** (`piotroski_f_score`, `altman_z_score`, `beneish_m_score`) — is the balance sheet and earnings quality behind the valuation trustworthy, or is the cheapness a symptom of deteriorating fundamentals.
5. **Only then, the assumption-heavy grids** (`two_stage_dcf`, `owner_earnings`, `three_scenario_expected_value`, and the relative-valuation models `own_history_percentile` / `peer_comparison`). These require you to pick or accept explicit growth/discount assumptions or a peer set — read them last, and read them as ranges, never as a single number.

**Never quote a single fair value.** Where a model returns a grid or a range, the report must carry the range and the assumptions behind it, not a collapsed point. A precise-looking number like "$94.32" reads as a fact when it is a function of guessed inputs — always show the grid (discount rate × growth rate, or bear/base/bull) alongside the number you're citing.

**When beta is unreliable** (the common case — check `valuation_inputs.derived.beta.reliable`; e.g. Zoom's R² of 0.055 on a ~6-month daily window), `router.profile.discount_rate_method` reads `sector_default_range`, not `capm_derived_range`: the discount rate is a documented sector-default *range*, never a CAPM point estimate. State in the report which method was used (`router.profile.discount_rate_method`) and quote the range, not a single rate.

### Worked example — Zoom (ZM), 2026-08-17

Price $105.96, market cap $31.81B (diluted 300.2M shares), EV $24.09B, net cash $7.72B (24.3% of cap), non-operating assets $1.88B (5.9%).

- `router`: beta unreliable → `sector_default_range` (9% / 11% / 13%). `enabled_models` = 11 of 12; `excluded` = `cash_runway` ("ttm_fcf is positive").
- `reverse_dcf`: implied FCF growth **−0.8% / +2.8% / +6.0%** at 9% / 11% / 13% discount rates — the market is pricing in roughly flat-to-modest growth, not a growth story and not a burn-out.
- `sum_of_the_parts`: implied core business **$75.87/share** (price $105.96 − net cash $25.72 − haircut non-op stake $4.37) — over 70% of the current price is core-business value, not cash.
- `net_cash_floor`: liquid assets **$25.72/share**, NCAV $21.26/share — the hard downside floor if the core business were worth zero.
- `owner_earnings`: SBC drag ratio **0.624** (headline FCF $1.961B → $1.223B ex-SBC) — any owner-earnings-based valuation should be read off the SBC-adjusted grid, not the headline one.
- `altman_z_score`: **10.61, "safe"** (Z'' variant — book equity used in X4, correct for this variant, not a bug).
- `piotroski_f_score`: **5/8** (denominator reduced, not padded, because Zoom has no long-term debt to score the leverage criterion against — read as 5/8, never "5/9").
- `three_scenario_expected_value`: bear **$73.92** / base **$124.93** / bull **$210.78** — report the three values and the probability weights, never the single probability-weighted number in isolation.
- Skipped: `cash_runway` (router: FCF positive — cash runway is meaningless when FCF is positive), `beneish_m_score` (3 required concepts not extracted from SEC data for this filer), `peer_comparison` (only produces output when same-layer peer contexts are available on this run — check `router.excluded` / the model's own `skip_reason` for whether peers were passed).

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
- `FINNHUB_API_KEY` — **OPTIONAL** (non-blocking). Free key from `finnhub.io`. Used only as a fallback for the analyst-targets lane when the primary Yahoo `quoteSummary` endpoint fails or returns empty. Without it, US tickers still work fine via Yahoo. Preflight emits a `[WARN]` (not `[FAIL]`) when this key is unset; that warning is purely informational.

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

Lead with **one action, not two verdicts.** The rule-engine state and the valuation gate result are inputs to the final action, not separate conclusions to present side by side — the reader needs a single `Reject` / `Watch` / `Starter` / `Add` / `Exit`, not a rule-engine verdict and a valuation verdict left for them to reconcile.

Lead with:

- conclusion
- action
- decision snapshot:
  - rule-engine score → suggested action (`rule_engine_precheck.state`, `total_score`)
  - valuation summary → gate result: `pass` (valuation does not restrain the action) or `downgrade` (valuation pulled the action down, and from what to what)
  - **final action** (post-gate; this is the one action from the lead)
  - when the final action is `Starter` or `Add`: a **position-size hint** tied to the margin of safety (e.g., current price vs. `net_cash_floor` / `sum_of_the_parts` / the bear case in `three_scenario_expected_value` — a thinner margin of safety implies a smaller starting size, never a fixed size regardless of valuation)

Then provide:

- event calendar
- trigger and attribution
- evidence by lane
- **valuation card**:
  - models used (`router.enabled_models`) and models excluded, with reasons (`router.excluded`)
  - key assumptions (discount-rate method and range, growth assumptions, peer set if used)
  - the value range produced (never a single number — quote the grid or the bear/base/bull spread)
  - where the current price sits inside that range
- risks and invalidation
- rerate conditions when relevant
- source links

Keep the answer in the user's language unless they ask otherwise.

### Agent Decision Record

Whenever you issue a `Watch`, `Starter`, or `Add` recommendation, you MUST also write a machine-readable decision file to `data/decisions/<run_date>/<TICKER>.decision.json` with this schema:

```json
{
  "ticker": "NVDA",
  "decision_date": "2026-05-16",
  "state": "STARTER",
  "reference_close": 102.4,
  "invalidate_conditions": [
    {"metric": "pct_from_reference", "comparator": "<=", "threshold": -0.08, "window": 2, "note": "stop loss"}
  ],
  "rerate_conditions": [
    {"metric": "days_to_earnings", "comparator": "<=", "threshold": 2, "window": 1, "note": "post-earnings re-judge"}
  ]
}
```

**Allowed metrics** (use exactly these):
- `close` — latest close price (absolute)
- `pct_from_reference` — (latest_close − reference_close) / reference_close
- `close_vs_sma20` — latest_close / sma20 − 1
- `new_low_20d` — 1.0 if latest close is a fresh 20-day low else 0.0
- `days_held` — trading days since decision_date
- `days_to_earnings` — trading/calendar days to next earnings (None-safe)

**Comparators:** `<`, `<=`, `>`, `>=`, `==`

**Valuation anchors are required when the valuation lane produced usable output.** All six allowed metrics above are relative (to a reference close, to SMA20, to a rolling low) or purely time-based — none of them expresses "what the business is worth." When `valuation_inputs.models` was not `null` for this ticker's review packet, at least one `invalidate_if` or `rerate_if` condition MUST be anchored to a valuation level rather than purely to price action. `close` already accepts an absolute threshold — use it with a valuation-derived number rather than inventing a new metric (e.g. `close <= <net_cash_floor per-share liquid assets>` or `close >= <a bull-case per-share value>`). For Zoom on 2026-08-17: `close <= 25.72` (liquid-asset floor breached — the thesis is broken) or `close >= 210` (bull-case value reached — re-evaluate taking profit). This is the first time the daily tracker can check a thesis against what the business is worth, rather than only against momentum — without it, every invalidation is a stop-loss and every re-rate is a technical trigger, and the thesis itself is never actually tested.

Only add a new metric to `decision_schema.py` if a valuation anchor genuinely cannot be expressed as an absolute `close` threshold — and if you do, implement its evaluation in the deterministic tracker code (`decision_tracker.py`), not only in the schema.

The `note` field is a human-readable annotation ONLY. It is NEVER evaluated; the structured `metric/comparator/threshold` fields are the operative rule enforced by deterministic pipeline code.

**Validation & enforcement:**
- After writing the file, validate it against the schema (well-formed JSON, required fields present, metrics in the allowed set). FIX and REWRITE in-session if malformed.
- `Starter`/`Add` require at least one `rerate_condition`; all three states require at least one `invalidate_condition`.
- A malformed file reaching the pipeline will be rejected with a visible warning and the recommendation will not be tracked.
