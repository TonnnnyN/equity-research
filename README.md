# Equity Research

An Agent Skill for single-name equity research on U.S.-listed companies. Evidence comes from deterministic Python pipelines that assemble SEC filings, price history, and fundamentals into a structured packet. The agent reads the packet and judges: reject, watch, start, add, or exit. No API keys required.

## Install and Run

Requires Python >= 3.11.

```bash
pip install -e .
export SEC_USER_AGENT="Your Name your@email.com"
equity-research --config config/default.toml review ZM NVDA
```

**CLI subcommands:**

- `init-db` — Initialize the SQLite database.
- `review TICKER [TICKER ...]` — Deep review on one or more tickers; emit review packets.
- `review-ticker TICKER` — Deep review on a single ad-hoc ticker; emit review packet.
- `valuation-order TICKER --order FILE` — Run valuation models against an order JSON file.
- `check-decisions` — Evaluate open decisions against fresh prices and report status.
- `show-report --date YYYY-MM-DD` — Display a saved markdown report.
- `preflight` — Validate runtime configuration for optional data providers.
- `cleanup-data [--date YYYY-MM-DD]` — Prune old reports and cached data per retention policy.

## Architecture

```
SEC EDGAR ──┐
Yahoo Finance┤
Stooq        └──→ [ Python Pipeline ]
                        ↓
                  Clean & Compute
                  (SEC quarter norms, valuation math)
                        ↓
                  Company Portrait +
                  Evidence Packet
                        ↓
                  [ Agent Reader ]
                  Reads facts, picks model order
                        ↓
                  [ Python Valuation ]
                  Runs ordered models (12 available)
                        ↓
                  Decision: Reject / Watch / Starter / Add / Exit
                        ↓
                  [ Outcome Ledger ]
                  Track invalidate_if / rerate_if conditions
                        ↓
                  Daily check-decisions sweep
                  (resolves outcomes, no manual re-run needed)
```

**Division of labour:** Python handles everything that would silently produce a wrong number if hand-written — SEC quarter normalisation, net-cash derivation, all valuation formulas. The agent does reading and reasoning: parsing disclosures, judging evidence quality, choosing which models to run and what assumptions to use, deciding the action and writing the invalidation/re-rating conditions.

## Data Sources

All free, no API keys:

- **SEC EDGAR**: 10-K, 10-Q, 8-K, company facts. Requires `SEC_USER_AGENT` environment variable (a "Name email@example.com" string SEC uses to identify your script).
- **Yahoo Finance**: Current price, historical bars, consensus targets, upgrade/downgrade history.
- **Stooq**: Fallback price history when Yahoo is unavailable or stale.
- **Social (optional)**: Agent fetches Reddit and X discussion URLs using its own browsing tools. If the environment blocks those sources, social data is marked unavailable and scoring adjusts automatically.

## Self-Evolution

Every decision is recorded with the bucket weights and valuation assumptions that produced it, plus explicit invalidate_if and rerate_if conditions.

**The tracking loop:**
- `check-decisions` runs daily as a cheap heartbeat. It evaluates those conditions across every trading day since the last check. A breach that happened on a day nobody manually ran still resolves on the day it happened at that day's close.
- Theses close as `invalidated` (condition fired, thesis broke), `rerated` (condition fired, thesis played out), or `expired` (held 90+ trading days without either). A `superseded` state exists when the agent changed its mind and is counted neither success nor failure.
- The ledger counts unique theses, not runs. A position re-derived monthly for a year is one thesis.

**Calibration evolves here:**
- `skills/equity-research/defaults/calibration.toml` holds bucket weights, action thresholds, and sample gates. The agent may edit it in-session.
- Every edit must cite outcomes: which tickers, what happened. Example: "Raised social_rebound weight 10→14 after tech-sentiment-driven pops in ASML, QCOM theses last month."
- Changes stay local (git-committed per run). The immutable rules that constrain the agent live in `SKILL.md`, which the agent reads but cannot edit.
- Calibration only adjusts when a subgroup of theses shows a departure from the base rate: at least five closed theses in the group, and observed outcomes > 1.5 sigma away from median. It reports counts and patterns, never a prescribed weight.
- Nothing changes for months. That is intended: a system that re-tunes weekly is drifting, not learning.

## Layout

```
skills/equity-research/
  SKILL.md
  defaults/calibration.toml
  agents/
  docs/
  references/

sharing-resources/
  src/equity_research/
    cli.py
    pipeline.py
    storage.py
    valuation.py
    sources/
  references/

config/default.toml
data/
  reports/
  raw/
  state/
```
