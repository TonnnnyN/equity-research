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
   Python: clean and compute
   (quarter normalisation, TTM, beta, price stats)
        |
   +----+------------------------+
   |                             |
 LAYER 1                      LAYER 2
 bucket scores                fundamentals, filings, price
 hard vetoes                  analyst targets, valuation inputs
   |                          four always-on metrics
   |                             |
   |                    Agent reads Layer 2 first
   |                    1. portrait: 10-K, then web for recent news
   |                    2. order: which models, which assumptions,
   |                       and which it declined, with reasons
   |                             |
   |                    Python runs exactly what was ordered
   |                             |
 Agent sets the six weights      |
 for this company                |
   |                             |
   +----------+------------------+
              |
      Agent judges, per SKILL.md
      valuation may lower the action, never raise it
      a hard veto caps it at Reject regardless
              |
      one action + invalidate / rerate conditions
              |
      check-decisions -> outcome ledger
```

Layer 1 and Layer 2 are layers of the evidence packet, not stages of the pipeline. Layer 1 is the mechanical bucket score and is advisory. Layer 2 is the evidence the agent reasons over, and the valuation models live there, which is why they never feed the bucket scores.

The agent chooses the models rather than Python choosing for it. It reads the filings, builds a portrait of the business, then names the models it wants and the assumptions to run them on. Python supplies no defaults: an order with no discount rate returns nothing for the models that need one. Piotroski, Altman, Beneish and the net-cash floor compute regardless, so a manipulation screen cannot be quietly skipped.

Python computes what hand-writing would get wrong in ways nobody notices: SEC quarter normalisation, net cash, every valuation formula. The agent does the reading and the judging.

## Bucket Scoring (110-point scale)

Fundamentals (30) covers financials, cash flow, balance-sheet strength. Risk_red_flags (20) catches bankruptcy, fraud, restatement, structural breaks. Chain_confirmation (20) checks sector/market context, peer comparison, macro. Sentiment (15) measures official disclosure tone. Price_flow (15) tracks post-drop stabilization and moving-average reclaim. Social_rebound (10) captures retail sentiment; scores only when sample ≥20.

Weights total 110 and scale dynamically when buckets drop. Agent may raise or lower any weight per company with stated justification.

## Data & Decisions

SEC EDGAR (10-K, 10-Q, 8-K) via `SEC_USER_AGENT`; Yahoo Finance (prices, targets, analyst history); Stooq (price fallback); optional social (Reddit, X).

Every decision stores weights, assumptions, and `invalidate_if`/`rerate_if` conditions. `check-decisions` evaluates daily. Theses resolve as `invalidated`, `rerated`, `expired` (90+ days), or `superseded`. Calibration lives in `defaults/calibration.toml`; edits must cite outcome evidence. Immutable rules in `SKILL.md` (agent reads, cannot edit).
