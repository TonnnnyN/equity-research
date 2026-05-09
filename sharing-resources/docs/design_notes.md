# Design Notes

## Product Intent

This repository supports two related but distinct Agent Skills:

- end-of-day market sentiment research for specific stocks
- broad U.S. small/mid-cap dislocation candidate screening

Both need shared runtime infrastructure, but they should not collapse into one Skill because they answer different questions.

## Design Principles

- Evidence should be saved before interpretation.
- Official disclosures should outrank social chatter.
- A severe price move should not automatically become a buy candidate.
- Scripts should handle deterministic, repeated operations.
- Skill references should explain judgment-heavy decisions.
- Secrets and generated data should stay out of Git.

## Why Two Skills

`market-sentiment-research` is a research-decision workflow. It can end with `Reject`, `Watch`, `Starter`, `Add`, or `Exit`.

`us-smallmid-dislocation` is a candidate-generation workflow. It can end with `Pass`, `Watchlist`, or `Investigate`.

Keeping those separate prevents a screening result from being mistaken for a portfolio action.

## Why Sharing Resources

The two Skills can share:

- the Python runtime engine
- tests
- local operating docs
- secrets policy
- generated data conventions
- JSON redaction

Those resources live under `sharing-resources/` so each Skill stays focused.

## Future Direction

- add a repeatable prepared-universe builder for U.S. small/mid screens
- add a target-pool sync script that converts `config/watchlist.toml` into `skills/market-sentiment-research/defaults/targets.toml`
- add richer local report indexing for previous decision audits
