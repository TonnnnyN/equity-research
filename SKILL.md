---
name: market-sentiment
description: Local Agent Skill for end-of-day market sentiment research, pullback review, and U.S. small/mid-cap dislocation screening. Use when Codex needs to run or maintain the market_sentiment Python pipeline, refresh price caches, inspect generated evidence packets, review stocks with Reject/Watch/Starter/Add decisions, screen beaten-down small/mid caps, or work with this project's scripts, defaults, references, docs, tests, and secrets policy.
---

# Market Sentiment

## What This Skill Is

This whole project is the skill.

Use it as the operating manual and resource bundle for a local, end-of-day market sentiment system. The skill combines:

- a Python runtime engine in `src/market_sentiment/`
- executable helper scripts in `scripts/`
- default watchlists and screening configs in `defaults/`
- task-specific references in `references/`
- project design and operating documents in `docs/`

The old standalone skill folders have been archived locally under `legacy_skills/`. Treat this root `SKILL.md` as the primary entry point.

## Directory Map

- `src/market_sentiment/`: production Python package and CLI engine.
- `config/watchlist.toml`: local runtime config used by the CLI.
- `defaults/`: portable skill defaults, including watchlist, target pool, and U.S. small/mid universe filters.
- `scripts/`: deterministic helper scripts for cache refresh and candidate screening.
- `references/`: load-on-demand operating references for workflows, scripts, configuration, outputs, and red flags.
- `docs/`: higher-level design notes and architecture explanations.
- `tests/`: regression tests for the Python engine.
- `data/`: generated local runtime artifacts. Do not treat it as skill source material unless the user asks to inspect a run.
- `secrets/`: local credentials and API keys. Never print actual values.
- `legacy_skills/`: local archive of pre-merge skill folders; not part of the portable skill source.

## First-Step Routing

When a user asks for:

- **a daily run, CLI issue, report, or pipeline bug**: read `references/script_detailed_reference.md` and `references/configuration_and_secrets.md`, then work in `src/market_sentiment/`, `config/`, and `tests/`.
- **a stock selloff review**: read `references/research_workflows.md` and `references/target_pool.md`; use local reports, review packets, target defaults, and live public sources when current facts are needed.
- **a broad U.S. small/mid-cap screen**: read `references/script_detailed_reference.md`, `references/smallmid_input_schema.md`, `references/smallmid_thresholds.md`, and `references/smallmid_red_flags.md`.
- **architecture or project structure questions**: read `docs/architecture.md` and `docs/design_notes.md`.
- **API keys, environment variables, or local credentials**: read `references/configuration_and_secrets.md`; do not expose secret values.
- **generated reports or historical runs**: read `references/data_and_outputs.md` before touching `data/`.

## Core Research Rules

- Trigger first, narrative second.
- Prefer primary evidence: SEC filings, IR materials, earnings releases, regulated disclosures, and official data.
- Treat social chatter as a weak secondary signal unless it is directly tied to measurable flow or a verifiable event.
- Separate "fell hard" from "fell to a level worth buying."
- Require both `invalidate_if` and `rerate_if` for constructive actions.
- Treat missing data as lower confidence, not permission to invent certainty.
- Keep conclusions traceable to price action, official disclosures, fundamentals, valuation, positioning, and catalysts.

## Runtime Workflow

For a normal daily-close run:

1. Source local secrets if needed: `source secrets/market_sentiment.secrets.sh`.
2. Install or refresh the editable package: `python3 -m pip install -e .`.
3. Run `market-sentiment --config config/watchlist.toml preflight`.
4. Run `market-sentiment --config config/watchlist.toml run-daily`.
5. Inspect `data/reports/<date>/report.md`, `report.json`, `review_queue.md`, `review_packets/`, and `manual_agent_report.zh.md`.

For code changes, run the focused tests first, then broaden to `python3 -m pytest` when the change touches shared pipeline, config, storage, scoring, or data-source behavior.

## Script Workflow

Read `references/script_detailed_reference.md` before using scripts.

- Refresh target-pool prices with `python3 scripts/update_price_cache.py`.
- Screen prepared U.S. small/mid CSVs with `python3 scripts/screen_candidates.py <input.csv> --output-json <out.json> --output-markdown <out.md>`.
- Redact sensitive JSON artifacts with `python3 scripts/redact_sensitive_json.py data`.

Do not rewrite large script logic inline when the bundled scripts already cover the task. Patch or parameterize the script instead.

## Secrets And Git Hygiene

- Actual local credentials belong only in `secrets/market_sentiment.secrets.sh` or another ignored file under `secrets/`.
- Keep examples in `secrets/*.example.sh` with placeholders only.
- Never print API keys, tokens, passwords, cookies, or account files in final answers.
- Do not commit generated cache or runtime data: `data/`, `.pytest_cache/`, `__pycache__/`, `.DS_Store`, `*.sqlite3`, `*.db`, and egg-info output.

## Output Style

Default to the user's language.

For investment research, lead with the actionable conclusion and then show the audit trail. For engineering work, lead with what changed, what was verified, and any remaining risk.
