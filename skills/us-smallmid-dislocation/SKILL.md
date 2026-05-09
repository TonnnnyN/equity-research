---
name: us-smallmid-dislocation
description: Candidate-generation Skill for U.S. small and mid-cap price dislocation triage. Use when Codex needs to screen a prepared CSV universe for Pass, Watchlist, or Investigate using market-cap, liquidity, structure, red-flag, fundamental-stability, and price-dislocation filters; not for final buy calls or single-stock portfolio actions.
---

# U.S. Small/Mid Dislocation

## Purpose

Use this Skill for broad candidate generation across a prepared U.S. small/mid-cap universe.

It answers:

Which names deserve more research because price action looks much worse than the recent business evidence?

This is not a final buy-rating Skill. Do not map `Investigate` directly to `Starter` or `Add`. For single-name selloff decisions, use `$market-sentiment-research`.

## Bundle Map

- `defaults/universe.toml`: screening thresholds, market-cap range, liquidity floors, excluded structures, and sector adjustments.
- `scripts/screen_candidates.py`: deterministic prepared-CSV screen.
- `references/input_schema.md`: required and recommended CSV columns.
- `references/thresholds.md`: trigger logic and state ceilings.
- `references/red_flags.md`: hard rejects, state ceilings, and value-trap patterns.
- `references/dislocation_workflow.md`: screening workflow and output expectations.
- `references/script_reference.md`: script-specific usage.
- `docs/design_notes.md`: why this Skill is candidate triage rather than portfolio research.

Shared project resources live outside this Skill:

- `../../sharing-resources/references/configuration_and_secrets.md`: secrets and environment policy.
- `../../sharing-resources/references/runtime_and_shared_scripts.md`: shared CLI and redaction script notes.
- `../../sharing-resources/scripts/redact_sensitive_json.py`: shared JSON redaction utility.

## Core Workflow

1. Start with universe hygiene: market cap, liquidity, listing quality, excluded structures, and special situations.
2. Apply hard red flags before ranking. Bankruptcy, going-concern, default, fraud, delisting, restatement, or severe filing issues usually mean `Pass`.
3. Require a primary dislocation signal plus a confirming signal before `Watchlist` or `Investigate`.
4. Use fundamental stability evidence to avoid false positives: revenue, margins, guidance, backlog, cash burn, leverage, filing freshness, and financing risk.
5. Rank candidates, then state what a deeper memo must verify.

States:

- `Pass`: filtered out, red-flagged, insufficient dislocation, or likely value trap.
- `Watchlist`: interesting but capped by quality, catalyst, liquidity, financing, or evidence gaps.
- `Investigate`: strong dislocation plus enough stability to justify deeper work.

The soft universe target can be around 2,800 names, but do not force the count. Quality of the prepared universe matters more than hitting a number.

## Script Path

The deterministic script only screens an existing CSV. It does not fetch the full market by itself.

```bash
python3 skills/us-smallmid-dislocation/scripts/screen_candidates.py input.csv \
  --output-json output/smallmid_results.json \
  --output-markdown output/smallmid_report.md
```

Before using the script, read:

- `references/input_schema.md`
- `references/thresholds.md`
- `references/red_flags.md`

## Output Shape

Lead with:

- run date
- universe summary
- filter count
- trigger count
- final shortlist

Then provide:

- candidate table
- priority list
- why the market may not be rewarding each stock
- red flags and state ceilings
- next research steps

Keep conclusions as screening states, not portfolio actions.
