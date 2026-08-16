---
name: us-smallmid-dislocation
description: Candidate-generation Skill for U.S. small and mid-cap price dislocation triage. Runs a three-stage pipeline: universe construction (build_universe.py), deterministic CSV screen (screen_candidates.py), and concurrent Sonnet subagent deep review per non-Pass candidate. Produces Pass, Watchlist, or Investigate states only; not for final buy calls or single-stock portfolio actions.
---

# U.S. Small/Mid Dislocation

## Purpose

Use this Skill for broad candidate generation across a U.S. small/mid-cap universe.

It answers:

Which names deserve more research because price action looks much worse than the recent business evidence?

This is not a final buy-rating Skill. Do not map `Investigate` directly to `Starter` or `Add`. For single-name selloff decisions, use `$market-sentiment-research`.

## Bundle Map

- `defaults/universe.toml`: screening thresholds, market-cap range, liquidity floors, excluded structures, sector adjustments, and universe-construction config.
- `scripts/build_universe.py`: assembles the universe CSV from SEC and Yahoo data sources.
- `scripts/screen_candidates.py`: deterministic prepared-CSV screen.
- `references/input_schema.md`: required and recommended CSV columns.
- `references/thresholds.md`: trigger logic and state ceilings.
- `references/red_flags.md`: hard rejects, state ceilings, and value-trap patterns.
- `references/dislocation_workflow.md`: three-stage screening workflow and output expectations.
- `references/subagent_workflow.md`: full stage-0/1/2 data flow and subagent verdict schema.
- `references/script_reference.md`: script-specific usage including `build_universe.py` and `review-ticker`.
- `docs/design_notes.md`: why this Skill is candidate triage rather than portfolio research.

Shared project resources live outside this Skill:

- `../../sharing-resources/references/configuration_and_secrets.md`: secrets and environment policy.
- `../../sharing-resources/references/runtime_and_shared_scripts.md`: shared CLI and redaction script notes.
- `../../sharing-resources/scripts/redact_sensitive_json.py`: shared JSON redaction utility.

## Three-Stage Pipeline Overview

This Skill runs three stages end-to-end. See `references/dislocation_workflow.md` for full detail.

**Stage 0 — Universe Construction**: `build_universe.py` pulls seed tickers from SEC, applies exchange/price/market-cap filters, fetches fundamentals for survivors, and writes `universe_<date>.csv`.

**Stage 1 — Deterministic Screen**: `screen_candidates.py` reads the CSV, applies all quality, red-flag, and dislocation filters, and writes `smallmid_results.json` with `Pass / Watchlist / Investigate` states.

**Stage 2 — Concurrent Subagent Deep Review**: the main agent reads `smallmid_results.json`, takes every candidate with `state != "Pass"`, and dispatches one Sonnet subagent per candidate in parallel. See the mandatory orchestration instructions below.

States:

- `Pass`: filtered out, red-flagged, insufficient dislocation, or likely value trap.
- `Watchlist`: interesting but capped by quality, catalyst, liquidity, financing, or evidence gaps.
- `Investigate`: strong dislocation plus enough stability to justify deeper work.

## Stage 0: Universe Construction

```bash
python3 skills/us-smallmid-dislocation/scripts/build_universe.py \
  --output data/universe/universe_<date>.csv

# Development smoke-test (first 20 tickers only):
python3 skills/us-smallmid-dislocation/scripts/build_universe.py \
  --limit 20 \
  --output data/universe/universe_test.csv
```

Read `references/input_schema.md` before inspecting the output CSV.

## Stage 1: Deterministic Screen

```bash
python3 skills/us-smallmid-dislocation/scripts/screen_candidates.py \
  data/universe/universe_<date>.csv \
  --output-json output/smallmid_results.json \
  --output-markdown output/smallmid_report.md
```

Before using the script, read:

- `references/input_schema.md`
- `references/thresholds.md`
- `references/red_flags.md`

## Stage 2: Mandatory Concurrent Subagent Deep Review

**This stage is mandatory whenever there are any non-Pass candidates from Stage 1.** The total evidence volume across many tickers grows linearly and would overload the main agent context; each subagent handles exactly one ticker in isolation.

### Orchestration instructions for the main agent

1. Read `smallmid_results.json`. Collect every entry where `state != "Pass"`. This is the candidate list for deep review.

2. For each candidate, dispatch **one Sonnet subagent** using a **parallel tool call** (send multiple Task tool calls in a single message). Batch in groups of **6–8** candidates per message to avoid rate limits. Wait for each batch to finish before dispatching the next.

3. Each subagent receives exactly this instruction:

   > You are a deep-review subagent for ticker `<T>` (screen state: `<screen_state>`).
   >
   > Step 1: Run `market-sentiment review-ticker <T>` to fetch the review packet.
   >
   > Step 2: Read the packet Layer 2 first (price_context, fundamentals_snapshot, official_events, social_summary, macro_summary, source_health). Form an independent view of fundamentals strength, disclosure tone, price flow, and red-flag risk before reading Layer 1 (bucket_scores, rule_engine_precheck, decision_summary). Apply the same Layer-2-first independent-scoring discipline described in the market-sentiment-research Skill.
   >
   > Step 3: Return ONLY the following compact JSON verdict and nothing else:
   >
   > ```json
   > {
   >   "ticker": "<T>",
   >   "screen_state": "<state from smallmid_results.json>",
   >   "refined_state": "<Pass|Watchlist|Investigate>",
   >   "conviction": "<high|medium|low>",
   >   "key_evidence": ["<concise Layer 2 evidence item>", "..."],
   >   "kill_risks": ["<risk that would make this a Pass>", "..."],
   >   "next_checks": ["<specific follow-up action>", "..."],
   >   "diverged_from_screen": false,
   >   "divergence_reason": null
   > }
   > ```
   >
   > If `refined_state` differs from `screen_state`, set `diverged_from_screen: true` and write in `divergence_reason` exactly which Layer 2 evidence drove the change.
   >
   > Do not produce a narrative report. Do not make a final portfolio action. Return the JSON verdict only.

4. After all batches complete, collect every subagent verdict. The main agent then aggregates them into the final candidate ranking report.

### Layer 2-first discipline (mandatory for all subagents)

Subagents must apply the same independent-scoring protocol as `market-sentiment-research`:

- Read Layer 2 raw evidence first. Form an independent view before consulting Layer 1.
- If `refined_state` diverges from `rule_engine_precheck.state`, the divergence must be grounded in specific Layer 2 facts (dates, numbers, filing excerpts).
- Hard vetos in `rule_engine_precheck.veto_reason` are binding regardless of other evidence.
- `bucket_scores` are mechanical and may be noisy; re-judge from raw `social_summary` when sample is small.

### What subagents must NOT do

- Subagents must not produce the final combined ranking or summary report. Only the main agent synthesizes across tickers.
- Subagents must not make portfolio actions (`Starter`, `Add`, `Exit`). Their output vocabulary is limited to `Pass / Watchlist / Investigate`.
- Subagents must not change the screening methodology or override Stage 1 results for all candidates; they may only refine the state for their single assigned ticker.

## Core Screening Workflow (Stage 1 logic)

1. Start with universe hygiene: market cap, liquidity, listing quality, excluded structures, and special situations.
2. Apply hard red flags before ranking. Bankruptcy, going-concern, default, fraud, delisting, restatement, or severe filing issues usually mean `Pass`.
3. Require a primary dislocation signal plus a confirming signal before `Watchlist` or `Investigate`.
4. Use fundamental stability evidence to avoid false positives: revenue, margins, guidance, backlog, cash burn, leverage, filing freshness, and financing risk.
5. Rank candidates, then state what a deeper memo must verify.

The soft universe target can be around 2,800 names, but do not force the count. Quality of the prepared universe matters more than hitting a number.

## Output Shape

Lead with:

- run date
- universe summary
- filter count
- trigger count
- final shortlist with subagent-refined states

Then provide:

- candidate table (screen state, refined state, conviction, key evidence)
- priority list (Investigate first, then Watchlist)
- divergence notes for any ticker where subagent refined state differs from screen state
- red flags and state ceilings
- next research steps per candidate

Keep conclusions as screening states, not portfolio actions.
