# Thresholds

Use these bands as default guidance, not as rigid math.

## Universe Hygiene

- Minimum price: `>= $5`
- Market cap: `~$300M-$20B`
- Average 20-day dollar volume: `>= $2M`

## Core Trigger Logic

Do not count pure price damage twice.
Escalate a name only when it clears both of these families:

### Primary dislocation family

At least `1` of:

- `52-week high drawdown >= 35%`
- `60-day drawdown >= 25%`

### Confirming family

At least `1` of:

- `60-day relative underperformance vs size-aware benchmark or sector ETF >= 15%`
- `good earnings, bad tape`

## Good Earnings, Bad Tape

Treat this as a valid trigger when:

- the company reported a non-broken quarter or update
- there is no guide cut, no financing warning, and no major negative `8-K`
- and total return from the pre-earnings close through `3-5` trading days after the event underperforms the chosen benchmark by at least `5%`
- or the stock is still flat to down while peers or the benchmark responded better

Do not use this rule when the release contains a clear guidance cut, financing stress signal, or major negative disclosure.

## Fundamental Stability Guide

Prefer names where most of the following are true:

- latest revenue trend is `>-5% YoY` or at least not in obvious collapse
- operating cash flow is positive
- normalized free cash flow is positive or close enough to breakeven that it does not imply funding stress
- cash is at least `~30%` of debt, or the company is in a net-cash posture
- cash runway is at least `~12 months`
- interest coverage is at least `~2x` when debt is meaningful
- share count growth is not signaling serial dilution
- a recent `10-Q`, `10-K`, `8-K`, earnings release, or investor deck exists within `~120` days
- positive cash flow is not mostly explained by working-capital release

These are default heuristics.
Sector context still matters.

### State Ceilings

Apply these ceilings even when the headline metrics look okay:

- `runway < 12 months => max Watchlist`
- `filings older than 120 days => max Watchlist`
- `share-count growth > 8% YoY => max Watchlist`
- `working-capital release ratio > 50% => max Watchlist`

## Sector Adjustments

### Software, recurring revenue, asset-light services

- allow slightly shallower dislocation bands such as `52-week drawdown >= 30%` or `60-day drawdown >= 22%`
- keep the relative-underperformance bar near `15%`
- be stricter on revenue deceleration and customer concentration

### Industrials, distributors, cyclical equipment

- allow somewhat wider raw-drawdown bands such as `60-day drawdown >= 28%`
- require more caution around inventory, backlog, and end-market slowdowns

### Consumer discretionary and retail

- keep the price trigger near the default bands
- add stricter working-capital and inventory skepticism, with a lower tolerance for cash flow boosted by payables or inventory release
- treat debt and lease obligations as higher-risk than the same leverage in stronger sectors

### Biotech, pre-profit medtech, and binary event stories

- exclude by default unless the user explicitly asks for them
- positive operating cash flow should not be waived casually

## Ranking Bias

Among the names that survive:

- rank higher when drawdown is severe but the official evidence still looks intact
- rank lower when the thesis depends on market mood reversing without a concrete catalyst
- rank much lower when the company may need financing even if the chart looks washed out
