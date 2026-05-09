# Research Workflows

## End-Of-Day Pullback Review

Answer one question for one stock or a short list:

Is this selloff worth ignoring, watching, starting, or adding to?

Default actions:

- `Reject`
- `Watch`
- `Starter`
- `Add`
- `Exit`

Core rules:

- Trigger first, narrative second.
- Primary-source evidence wins when sources disagree.
- A cheap valuation without a catalyst is not enough.
- `Watch`, `Starter`, and `Add` need explicit `invalidate_if`.
- `Starter` and `Add` need explicit `rerate_if`.

## Source Priority

1. SEC filings, IR materials, earnings releases
2. Operating data, regulated disclosures, primary corporate documents
3. Reputable financial news and industry reporting
4. Valuation aggregators, analyst estimates, EPS revisions
5. Positioning data, insider activity, options data
6. Macro and commodity context
7. Social media, forums, and general web chatter

Lower-priority sources should not overrule higher-priority sources without a stated reason.

## Evidence Lanes

Use these lanes for full reviews:

- price and technical context
- official disclosures
- fundamentals, valuation, revisions, and peer comparison
- positioning and flow
- catalyst path
- bear check

Keep the final action on the orchestrator side. Sub-agents may summarize raw filings, broad news scans, social scans, and peer data, but the final synthesis should not be delegated.

## Event Calendar Precheck

Check `[T-3, T+5]` for:

- earnings
- FOMC, CPI, PCE, NFP
- FDA PDUFA or AdCom
- expected legal or regulatory rulings
- capital actions

If a major catalyst sits inside the window, cap the action at `Watch` unless the move is already explained by published disclosure.

## Force Reject

Usually force `Reject` when any of these are credibly present:

- bankruptcy, default, going-concern, fraud, restatement, or credible investigation risk
- guidance cut or structural demand break confirmed by official evidence, plus fresh lows
- cheap valuation with still-falling revisions and deteriorating official evidence
- thesis depends almost entirely on social chatter

## Cap At Watch

Cap at `Watch` when:

- coverage is thin
- trigger is weak
- the name is in uncontrolled fresh lows
- sector or market pressure dominates and idiosyncratic edge is unclear
- a major scheduled catalyst sits in `[T-3, T+5]`
- peer comparison is clearly worse on multiple dimensions
- confidence is below medium

## U.S. Small/Mid Dislocation Triage

Use this workflow for candidate generation across a broad prepared universe. It is not a buy call.

Default states:

- `Pass`
- `Watchlist`
- `Investigate`

The screen should identify companies whose prices have broken much harder than their recent fundamentals, while filtering out low-quality microcaps, special situations, severe dilution risk, stale filings, refinancing pressure, and likely value traps.

For the deterministic script path, read:

- `references/smallmid_input_schema.md`
- `references/smallmid_thresholds.md`
- `references/smallmid_red_flags.md`

## Output Shape For Research

For a single-name pullback review, lead with:

- conclusion
- action
- decision snapshot

Then provide:

- event calendar
- trigger and attribution
- evidence
- risks and invalidation
- rerate conditions when relevant
- source links

For broad screens, lead with:

- run date
- universe summary
- filter count
- trigger count
- final shortlist
- candidate table
- priority list
- why the market may not be rewarding the stock
- key risks
- next steps
