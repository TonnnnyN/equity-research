# Design Notes

## Product Intent

This repository supports one Agent Skill: end-of-day market sentiment research for specific stocks.

It previously also carried a second Skill, `us-smallmid-dislocation`, for broad U.S. small/mid-cap
dislocation candidate screening. That Skill was removed (its history remains on the `smallmid-dev`
branch — see `docs/architecture.md` "What Changed"); the shared-runtime split below is kept because
the runtime engine is still reusable by a future screening Skill, not because one currently exists.

## Design Principles

- Evidence should be saved before interpretation.
- Official disclosures should outrank social chatter.
- A severe price move should not automatically become a buy candidate.
- Scripts should handle deterministic, repeated operations.
- Skill references should explain judgment-heavy decisions.
- Secrets and generated data should stay out of Git.

## Scope

`market-sentiment-research` is a research-decision workflow. It can end with `Reject`, `Watch`, `Starter`, `Add`, or `Exit`.

## Why Sharing Resources

Keeping this infrastructure under `sharing-resources/` rather than inside the Skill folder means any
future additional Skill (e.g. a revived small/mid screening Skill) could reuse:

- the Python runtime engine
- tests
- local operating docs
- secrets policy
- generated data conventions
- JSON redaction

## Future Direction

- add richer local report indexing for previous decision audits
