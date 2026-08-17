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
Data sources ──→ [ Python Pipeline ] ──→ [ Evidence Packet ]
  (SEC, Yahoo,         ↓
   Stooq)          Compute SEC norms,
                   valuation math,
                   four always-on metrics
                        ↓
              ┌─────────────────────┐
              │                     │
         LAYER 1:             LAYER 2:
         Bucket Scores        Always-Ons & Raw Evidence
         (6 buckets,          (Piotroski, Altman, Beneish,
          hard vetos)          net-cash floor,
              │                 price bars, macro)
              │                     │
              └──→ [ Agent Reader ]─┘
                   Sets bucket weights,
                   picks valuation model order
                        ↓
              [ Python Valuation ]
                        ↓
         Hard Vetoes (fraud, bankruptcy) → REJECT
                        ↓
         Valuation Gate (can lower, never raise)
                        ↓
         Decision: Reject / Watch / Starter / Add / Exit
```

Layer 1 is mechanical bucket scoring (advisory only). Layer 2 holds always-on metrics and raw evidence the agent reasons over. Python computes what hand-writing would butcher: SEC norms, net cash, valuations. The agent handles reading evidence, setting weights per company, picking models, deciding action, and writing condition checks.

## Bucket Scoring (110-point scale)

Fundamentals (30) covers financials, cash flow, balance-sheet strength. Risk_red_flags (20) catches bankruptcy, fraud, restatement, structural breaks. Chain_confirmation (20) checks sector/market context, peer comparison, macro. Sentiment (15) measures official disclosure tone. Price_flow (15) tracks post-drop stabilization and moving-average reclaim. Social_rebound (10) captures retail sentiment; scores only when sample ≥20.

Weights total 110 and scale dynamically when buckets drop. Agent may raise or lower any weight per company with stated justification.

## Data & Decisions

SEC EDGAR (10-K, 10-Q, 8-K) via `SEC_USER_AGENT`; Yahoo Finance (prices, targets, analyst history); Stooq (price fallback); optional social (Reddit, X).

Every decision stores weights, assumptions, and `invalidate_if`/`rerate_if` conditions. `check-decisions` evaluates daily. Theses resolve as `invalidated`, `rerated`, `expired` (90+ days), or `superseded`. Calibration lives in `defaults/calibration.toml`; edits must cite outcome evidence. Immutable rules in `SKILL.md` (agent reads, cannot edit).
