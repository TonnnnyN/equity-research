# Dislocation Workflow

## Goal

Find U.S. small/mid-cap candidates where price action appears disconnected from recent business evidence.

This workflow produces screening states only:

- `Pass`
- `Watchlist`
- `Investigate`

It does not produce `Reject`, `Starter`, `Add`, or other portfolio actions.

## Universe First

Before scoring a candidate, remove names with unsuitable structure or weak tradability:

- market cap outside the configured range
- weak average dollar volume
- OTC, SPAC shell, closed-end fund, ETF, ADR when excluded by config
- stale filings or missing core fields
- hard special situations that distort price action

The universe size is a soft target, not a requirement.

## Trigger Logic

Require both:

- a primary dislocation family, such as large drawdown, fresh lows, high downside from moving averages, or strong benchmark-relative underperformance
- a confirming family, such as good earnings with bad tape, stable fundamentals, revision resilience, or sector-adjusted disconnect

Use `references/thresholds.md` for exact threshold guidance.

## Red-Flag Order

Apply red flags before ranking:

1. Hard red flags usually force `Pass`.
2. Strong negative signals usually cap at `Watchlist`.
3. Structural distortions require explicit notes.
4. Value-trap patterns reduce priority unless the candidate has a concrete near-term repair path.

Use `references/red_flags.md` for detailed examples.

## Escalation

`Investigate` means the name deserves deeper research. The next step should verify:

- latest filing and balance-sheet risk
- exact event that caused the move
- whether guidance, revisions, or backlog actually remain stable
- liquidity and tradability
- upcoming catalysts
- peer-relative valuation and quality
