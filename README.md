# Equity Research

Agent Skill for single-name equity research on U.S.-listed companies. Python assembles SEC filings, price history, and fundamentals into a packet; the agent reads it and decides: reject, watch, start, add, or exit. No API keys required.

## Install

Requires Python >= 3.11. Set your SEC_USER_AGENT (required by SEC EDGAR):

```bash
pip install -e .
export SEC_USER_AGENT="Your Name your@email.com"
equity-research review ZM NVDA
```

Subcommands: `init-db`, `review TICKER [...]`, `review-ticker TICKER`, `valuation-order TICKER --order FILE`, `check-decisions`, `show-report --date YYYY-MM-DD`, `preflight`, `cleanup-data`.

## Architecture

```
          SEC / Yahoo / Stooq
                   |
          Python cleans and computes
          quarters, TTM, beta, price stats
          Piotroski, Altman, Beneish, net cash, always,
          so a fraud screen cannot be quietly skipped
                   |
     +-------------+-----------------------+
     |                                     |
  LAYER 1                             LAYER 2
  fundamentals                        filings, price, targets,
  risk flags                          model inputs
  chain                                    |
  sentiment                           agent reads the 10-K,
  price flow                          then the web
  social                                   |
     |                                agent picks the models and
  agent sets the six                  the assumptions, and says
  weights for this ticker             which it skipped and why
     |                                     |
     |                                Python runs that order
     |                                     |
     +-------------+-----------------------+
                   |
          agent decides, per SKILL.md
          valuation can lower the call, never raise it
          a hard veto forces Reject
                   |
          one call + what would kill it
                   |
          check-decisions -> outcome ledger
```

Layer 1 and Layer 2 are layers of the evidence packet, not stages of a pipeline. Layer 1 is a score, not a verdict. Layer 2 is what the agent reasons over, and the valuation models sit there, which is why they never feed the bucket scores.

The agent picks the models rather than Python picking for it. It reads the filings, builds a portrait of the business, then names the models it wants and the assumptions to run them on. Python supplies no defaults: an order with no discount rate returns nothing for the models that need one. What Python does own is the arithmetic that goes wrong in ways nobody notices, such as SEC quarter normalisation, net cash, and every valuation formula.

## Bucket Scoring (110-point scale)

Fundamentals (30) covers financials, cash flow, balance-sheet strength. Risk_red_flags (20) catches bankruptcy, fraud, restatement, structural breaks. Chain_confirmation (20) checks sector/market context, peer comparison, macro. Sentiment (15) measures official disclosure tone. Price_flow (15) tracks post-drop stabilization and moving-average reclaim. Social_rebound (10) captures retail sentiment; scores only when sample ≥20.

Weights total 110 and scale dynamically when buckets drop. Agent may raise or lower any weight per company with stated justification.

## Data & Decisions

SEC EDGAR (10-K, 10-Q, 8-K) via `SEC_USER_AGENT`; Yahoo Finance (prices, targets, analyst history); Stooq (price fallback); optional social (Reddit, X).

Every decision stores weights, assumptions, and `invalidate_if`/`rerate_if` conditions. `check-decisions` evaluates daily. Theses resolve as `invalidated`, `rerated`, `expired` (90+ days), or `superseded`. Default weights and valuation habits live in `defaults/calibration.toml`. Immutable rules live in `SKILL.md`, which the agent reads but cannot edit.
