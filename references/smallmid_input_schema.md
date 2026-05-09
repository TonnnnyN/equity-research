# Input Schema

`scripts/screen_candidates.py` expects a prepared CSV.
It does not fetch the universe for you.
Use it after you have already assembled a broad small/mid-cap export from public or local sources.

## Required Columns

- `ticker`
- `name`
- `exchange`
- `market_cap_usd`
- `price`
- `avg_dollar_volume_20d_usd`
- `drawdown_52w`
- `drawdown_60d`
- `relative_underperformance_60d`

Ratios may be provided either as decimals like `0.35` or percentages like `35`.

## Strongly Recommended Columns

- `country`
- `security_type`
- `structure`
- `sector`
- `industry`
- `benchmark`
- `flags`

Use `flags` as a comma-separated list, for example:

`guidance_cut,dilution_risk,late_filing`

## Fundamental Stability Columns

Add as many of these as you can:

- `revenue_yoy`
- `operating_cashflow_latest`
- `normalized_fcf_latest`
- `cash_latest`
- `debt_latest`
- `runway_months`
- `interest_coverage`
- `filing_age_days`
- `share_count_growth_yoy`
- `working_capital_release_ratio`

## Event / Tape Columns

These make the `good_earnings_bad_tape` rule much more useful:

- `earnings_days_ago`
- `earnings_quality`
- `earnings_price_confirmation`
- `post_earnings_relative_return_5d`

## Special Situation Flags

Use these tags when relevant:

- `recent_despac`
- `reverse_split`
- `pre_revenue_biotech`
- `precommercial_medtech`
- `strategic_review`
- `busted_deal`
- `going_concern`
- `default`
- `delisting`
- `fraud`
- `restatement`
- `material_weakness`
- `guidance_cut`
- `dilution_risk`
- `secondary_offering`
- `refinancing_pressure`
- `exchange_compliance_notice`
- `late_filing`

## Example Row

```csv
ticker,name,exchange,market_cap_usd,price,avg_dollar_volume_20d_usd,drawdown_52w,drawdown_60d,relative_underperformance_60d,revenue_yoy,operating_cashflow_latest,normalized_fcf_latest,cash_latest,debt_latest,runway_months,interest_coverage,filing_age_days,share_count_growth_yoy,working_capital_release_ratio,earnings_days_ago,earnings_quality,earnings_price_confirmation,post_earnings_relative_return_5d,flags
ABCD,Example Corp,NASDAQ,1800000000,12.4,8500000,42,29,17,3,42000000,18000000,210000000,280000000,18,3.1,35,4,0.20,12,good,weak,-6,
```
