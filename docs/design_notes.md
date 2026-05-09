# Design Notes

## Product Intent

The system is a local-first, end-of-day research assistant for long-biased equity review. It is designed to answer:

- Which watched names had an abnormal move?
- Was the move company-specific, sector-wide, or market-wide?
- Does the evidence support `Reject`, `Watch`, `Starter`, `Add`, or `Exit`?
- Which cases deserve a deeper manual Agent review?

The goal is not high-frequency trading. The goal is repeatable evidence capture and decision hygiene.

## Design Principles

- Evidence beats narrative.
- A drawdown is only a trigger, not a thesis.
- Official disclosures outrank news, social media, and valuation aggregators.
- Missing data lowers confidence.
- Generated data should be reproducible or at least traceable to a source payload.
- The skill should guide Codex without forcing all details into the context window.

## Why A Root Skill

The useful capability is the whole project, not only the earlier `market-sentiment-research` folder.

The Python package can run the daily pipeline. The scripts refresh caches and screen prepared universes. The references tell Codex how to interpret outputs, handle sources, and apply guardrails. The docs preserve design intent.

Keeping `SKILL.md` at the root makes the project installable as one Agent Skill while preserving the engine, tests, and local operations in the same folder.

## Separation Of Concerns

- `SKILL.md`: navigation and compact rules.
- `references/`: detailed workflow knowledge.
- `scripts/`: deterministic operations.
- `src/`: reusable Python engine.
- `docs/`: design thinking and project-level explanations.
- `secrets/`: private local credentials.
- `data/`: generated artifacts and cache.

## Future Direction

Useful next improvements:

- add a script that summarizes the latest `data/reports/<date>/review_packets/` into a compact Agent task list
- add a target-pool sync script that converts `config/watchlist.toml` into `defaults/targets.toml`
- add a lightweight installer that creates or refreshes a symlink from this project into `~/.codex/skills/market-sentiment`
- split research output contracts into separate references if they grow too large
