from __future__ import annotations

from datetime import date

from equity_research.models import (
    BetaEstimate,
    ConceptHistory,
    PriceBar,
    ProvenancedValue,
    ValuationDerived,
    ValuationFundamentals,
)

_MIN_BETA_OBSERVATIONS = 5
_MAX_CONTIGUOUS_QUARTER_GAP_DAYS = 100
_MIN_RELIABLE_R_SQUARED = 0.10
_MIN_RELIABLE_OBSERVATIONS = 30


def compute_valuation_derived(
    ticker: str,
    as_of: date,
    fundamentals: ValuationFundamentals | None,
    prices: list[PriceBar],
    benchmark_prices: list[PriceBar],
) -> ValuationDerived:
    """Compute Layer 2 derived valuation inputs. No models, no verdicts.

    Every field is a ``ProvenancedValue`` (or ``BetaEstimate``) stating which raw
    inputs and source produced it; missing inputs yield ``value=None`` with a
    ``reason`` and are appended to ``data_gaps``, never silently defaulted.
    """
    data_gaps: list[str] = []

    ttm_revenue = _concept_ttm(fundamentals, "revenue", data_gaps)
    ttm_gross_profit = _concept_ttm(fundamentals, "gross_profit", data_gaps)
    ttm_operating_income = _concept_ttm(fundamentals, "operating_income", data_gaps)
    ttm_net_income = _concept_ttm(fundamentals, "net_income", data_gaps)
    ttm_operating_cashflow = _concept_ttm(fundamentals, "operating_cashflow", data_gaps)
    ttm_capex_signed = _concept_ttm(fundamentals, "capex", data_gaps)
    ttm_sbc = _concept_ttm(fundamentals, "share_based_compensation", data_gaps)
    ttm_da = _concept_ttm(fundamentals, "depreciation_amortization", data_gaps)
    ttm_income_tax_expense = _concept_ttm(fundamentals, "income_tax_expense", data_gaps)

    ttm_capex = _abs_value(ttm_capex_signed)

    ttm_fcf = _combine(
        ttm_operating_cashflow, ttm_capex, lambda a, b: a - b,
        "ttm_operating_cashflow - |ttm_capex|", "ttm_fcf", data_gaps,
    )
    ttm_fcf_ex_sbc = _combine(
        ttm_fcf, ttm_sbc, lambda a, b: a - b,
        "ttm_fcf - ttm_sbc", "ttm_fcf_ex_sbc", data_gaps,
    )
    ttm_ebitda = _combine(
        ttm_operating_income, ttm_da, lambda a, b: a + b,
        "ttm_operating_income + ttm_da", "ttm_ebitda", data_gaps,
    )
    ttm_pretax_income_implied = _combine(
        ttm_net_income, ttm_income_tax_expense, lambda a, b: a + b,
        "ttm_net_income + ttm_income_tax_expense (implied pretax income; net income "
        "may also include non-operating gains/losses, see net_income vs operating_income caveat)",
        "ttm_pretax_income_implied", data_gaps,
    )
    effective_tax_rate = _divide(
        ttm_income_tax_expense, ttm_pretax_income_implied,
        "ttm_income_tax_expense / ttm_pretax_income_implied", "effective_tax_rate", data_gaps,
    )

    cash = _instant_latest(fundamentals, "cash_and_equivalents", data_gaps)
    short_term_investments = _instant_latest(fundamentals, "short_term_investments", data_gaps)
    long_term_investments = _instant_latest(fundamentals, "long_term_investments", data_gaps)
    # total_liquid_assets = cash + CURRENT marketable securities only. Noncurrent
    # ("long_term_investments") is deliberately excluded: the us-gaap tags filers use for
    # it are ambiguous across filers — some genuinely mean noncurrent marketable
    # securities, but for others (e.g. Zoom's LongTermInvestments) it is a strategic /
    # venture equity-stake bucket that is not liquid at all. See non_operating_assets
    # below, which is where illiquid strategic stakes belong instead.
    total_liquid_assets = _sum_optional(
        [cash, short_term_investments],
        zero_if_missing={1},
        basis="cash_and_equivalents + short_term_investments",
        field_name="total_liquid_assets",
        data_gaps=data_gaps,
    )
    non_operating_assets = _instant_latest(fundamentals, "non_operating_assets", data_gaps)

    long_term_debt = _instant_latest(fundamentals, "long_term_debt", data_gaps)
    finance_lease_obligations = _instant_latest(fundamentals, "finance_lease_obligations", data_gaps)
    total_debt = _sum_optional(
        [long_term_debt, finance_lease_obligations],
        zero_if_missing={0, 1},
        basis="long_term_debt + finance_lease_obligations",
        field_name="total_debt",
        data_gaps=data_gaps,
    )
    net_cash = _combine(
        total_liquid_assets, total_debt, lambda a, b: a - b,
        "total_liquid_assets - total_debt", "net_cash", data_gaps,
    )

    shares_used = _resolve_shares(fundamentals, data_gaps)
    market_cap = _resolve_market_cap(shares_used, prices, data_gaps)
    enterprise_value = _combine(
        market_cap, net_cash, lambda a, b: a - b,
        "market_cap - net_cash", "enterprise_value", data_gaps,
    )

    book_value = _instant_latest(fundamentals, "stockholders_equity", data_gaps)
    total_current_assets = _instant_latest(fundamentals, "total_current_assets", data_gaps)
    total_current_liabilities = _instant_latest(fundamentals, "total_current_liabilities", data_gaps)
    working_capital = _combine(
        total_current_assets, total_current_liabilities, lambda a, b: a - b,
        "total_current_assets - total_current_liabilities", "working_capital", data_gaps,
    )
    current_ratio = _divide(
        total_current_assets, total_current_liabilities,
        "total_current_assets / total_current_liabilities", "current_ratio", data_gaps,
    )

    beta = compute_beta(prices, benchmark_prices)
    if beta.beta is None or not beta.reliable:
        data_gaps.append(f"beta: {beta.reason}")

    # Attribute every gap collected above as derivation-level (computed here, in Layer 2:
    # missing inputs, non-contiguous quarters, no price bars, unreliable beta, ...) before
    # merging in extraction-level gaps from ValuationFundamentals (sources/sec.py: stale
    # SEC tag fallbacks, missing companyfacts concepts, ...). Keeping the two prefixes
    # distinct is deliberate — a reader must be able to tell "this concept came from a tag
    # last used years ago" (an extraction problem, fixable only by SEC filers reporting a
    # fresher tag) apart from "market cap unavailable: no price bars" (a derivation
    # problem, this pipeline's own price lane). Merging without attribution would blur
    # exactly the distinction this function exists to preserve.
    data_gaps[:] = [f"derivation: {gap}" for gap in data_gaps]
    if fundamentals is not None:
        data_gaps.extend(f"extraction: {gap}" for gap in fundamentals.data_gaps)

    return ValuationDerived(
        ticker=ticker,
        as_of=as_of,
        shares_used=shares_used,
        market_cap=market_cap,
        cash_and_equivalents=cash,
        short_term_investments=short_term_investments,
        long_term_investments=long_term_investments,
        total_liquid_assets=total_liquid_assets,
        non_operating_assets=non_operating_assets,
        total_debt=total_debt,
        net_cash=net_cash,
        enterprise_value=enterprise_value,
        ttm_revenue=ttm_revenue,
        ttm_gross_profit=ttm_gross_profit,
        ttm_operating_income=ttm_operating_income,
        ttm_net_income=ttm_net_income,
        ttm_operating_cashflow=ttm_operating_cashflow,
        ttm_capex=ttm_capex,
        ttm_sbc=ttm_sbc,
        ttm_da=ttm_da,
        ttm_fcf=ttm_fcf,
        ttm_fcf_ex_sbc=ttm_fcf_ex_sbc,
        ttm_ebitda=ttm_ebitda,
        ttm_income_tax_expense=ttm_income_tax_expense,
        ttm_pretax_income_implied=ttm_pretax_income_implied,
        effective_tax_rate=effective_tax_rate,
        book_value=book_value,
        working_capital=working_capital,
        current_ratio=current_ratio,
        beta=beta,
        data_gaps=data_gaps,
    )


# ---------------------------------------------------------------------------
# Concept lookups
# ---------------------------------------------------------------------------

def _stale_caveat(fundamentals: ValuationFundamentals | None, concept: str) -> str | None:
    """The fundamentals-level stale-tag-fallback entry for ``concept``, if sources/sec.py's
    tag selection had to fall back to a candidate tag outside its freshness window for it
    (see ``sec.py``'s ``_STALE_TAG_WINDOW_DAYS`` / ``_select_tag_order``). Returns the raw
    ``ValuationFundamentals.data_gaps`` entry text (already concept-attributed and
    self-describing) so callers can attach it directly to the ``ProvenancedValue`` they
    build from this concept, letting the caveat travel with the number instead of only
    being discoverable by cross-referencing a list far away from it."""
    if fundamentals is None:
        return None
    prefix = f"{concept}: stale tag fallback ("
    for gap in fundamentals.data_gaps:
        if gap.startswith(prefix):
            return gap
    return None


def _concept_ttm(
    fundamentals: ValuationFundamentals | None, concept: str, data_gaps: list[str]
) -> ProvenancedValue:
    if fundamentals is None or concept not in fundamentals.concepts:
        data_gaps.append(f"{concept}: no data from SEC companyfacts")
        return ProvenancedValue(value=None, source="sec_companyfacts", reason="missing_concept")
    history: ConceptHistory = fundamentals.concepts[concept]
    points = history.datapoints[:4]
    if len(points) < 4:
        reason = f"only {len(points)} quarterly datapoints available, need 4 for TTM"
        data_gaps.append(f"{concept}: {reason}")
        return ProvenancedValue(
            value=None,
            inputs=[p.end.isoformat() for p in points],
            source=f"sec_companyfacts:{history.tag}",
            basis="sum_last_4_quarters",
            reason=reason,
        )
    max_gap_days = max(
        (points[i].end - points[i + 1].end).days for i in range(len(points) - 1)
    )
    if max_gap_days > _MAX_CONTIGUOUS_QUARTER_GAP_DAYS:
        # sources/sec.py's quarterization normaliser (_quarterize_duration_entries)
        # already derives a missing Q4 (FY - 9M) or a YTD-tagged Q2/Q3 (H1 - Q1, 9M - H1)
        # from cumulative facts, so this should be rare — it means even that derivation
        # could not bridge the gap (e.g. a fiscal-year-end change, or a genuinely missing
        # cumulative fact to subtract from). A non-contiguous sum would silently mix
        # unrelated periods, so it is never returned as if it were a real TTM.
        reason = (
            f"4 most recent quarterly datapoints "
            f"({[p.end.isoformat() for p in points]}) are not contiguous (max gap "
            f"{max_gap_days}d) even after quarter derivation — cannot build a reliable TTM"
        )
        data_gaps.append(f"{concept}: {reason}")
        return ProvenancedValue(
            value=None,
            inputs=[p.end.isoformat() for p in points],
            source=f"sec_companyfacts:{history.tag}",
            basis="sum_last_4_quarters",
            reason=reason,
        )
    total = sum(p.value for p in points)
    inputs = [
        f"{p.end.isoformat()}{' (derived: ' + p.derived_from + ')' if p.derived and p.derived_from else ''}"
        for p in points
    ]
    basis = "sum_last_4_quarters"
    if any(p.derived for p in points):
        basis += " (includes derived quarter(s), see inputs)"
    caveat = _stale_caveat(fundamentals, concept)
    return ProvenancedValue(
        value=total,
        inputs=inputs,
        source=f"sec_companyfacts:{history.tag}",
        basis=basis,
        caveats=[caveat] if caveat else [],
    )


def _instant_latest(
    fundamentals: ValuationFundamentals | None, concept: str, data_gaps: list[str]
) -> ProvenancedValue:
    if fundamentals is None or concept not in fundamentals.concepts:
        data_gaps.append(f"{concept}: no data from SEC companyfacts")
        return ProvenancedValue(value=None, source="sec_companyfacts", reason="missing_concept")
    history = fundamentals.concepts[concept]
    if not history.datapoints:
        data_gaps.append(f"{concept}: concept matched but has no datapoints")
        return ProvenancedValue(value=None, source=f"sec_companyfacts:{history.tag}", reason="empty_history")
    dp = history.datapoints[0]
    caveat = _stale_caveat(fundamentals, concept)
    return ProvenancedValue(
        value=dp.value,
        inputs=[dp.end.isoformat()],
        source=f"sec_companyfacts:{history.tag}",
        basis="latest_reported_period_end",
        caveats=[caveat] if caveat else [],
    )


def _abs_value(value: ProvenancedValue) -> ProvenancedValue:
    if value.value is None:
        return value
    return ProvenancedValue(
        value=abs(value.value), inputs=value.inputs, source=value.source, basis=value.basis,
        caveats=value.caveats,
    )


def _merge_caveats(*groups: list[str]) -> list[str]:
    """Order-preserving de-duplicated union of caveat lists, so a caveat picked up from a
    shared upstream concept (e.g. a stale share count feeding both ``market_cap`` and,
    through it, ``enterprise_value``) does not pile up into repeated copies as it travels
    through several layers of combination."""
    merged: list[str] = []
    for group in groups:
        for caveat in group:
            if caveat not in merged:
                merged.append(caveat)
    return merged


# ---------------------------------------------------------------------------
# Arithmetic helpers — every combination carries provenance and never defaults
# a missing input to zero unless the caller explicitly opts in (zero_if_missing).
# ---------------------------------------------------------------------------

def _combine(
    a: ProvenancedValue,
    b: ProvenancedValue,
    fn,
    basis: str,
    field_name: str,
    data_gaps: list[str],
) -> ProvenancedValue:
    caveats = _merge_caveats(a.caveats, b.caveats)
    if a.value is None or b.value is None:
        missing = [part for part in (a.reason, b.reason) if part]
        reason = f"missing inputs: {', '.join(missing) if missing else 'unknown'}"
        data_gaps.append(f"{field_name}: {reason}")
        return ProvenancedValue(
            value=None, inputs=a.inputs + b.inputs, source=f"{a.source}|{b.source}", basis=basis, reason=reason,
            caveats=caveats,
        )
    return ProvenancedValue(
        value=fn(a.value, b.value), inputs=a.inputs + b.inputs, source=f"{a.source}|{b.source}", basis=basis,
        caveats=caveats,
    )


def _divide(
    numerator: ProvenancedValue,
    denominator: ProvenancedValue,
    basis: str,
    field_name: str,
    data_gaps: list[str],
) -> ProvenancedValue:
    caveats = _merge_caveats(numerator.caveats, denominator.caveats)
    if numerator.value is None or denominator.value is None:
        missing = [part for part in (numerator.reason, denominator.reason) if part]
        reason = f"missing inputs: {', '.join(missing) if missing else 'unknown'}"
        data_gaps.append(f"{field_name}: {reason}")
        return ProvenancedValue(
            value=None,
            inputs=numerator.inputs + denominator.inputs,
            source=f"{numerator.source}|{denominator.source}",
            basis=basis,
            reason=reason,
            caveats=caveats,
        )
    if denominator.value == 0:
        reason = "division by zero"
        data_gaps.append(f"{field_name}: {reason}")
        return ProvenancedValue(
            value=None,
            inputs=numerator.inputs + denominator.inputs,
            source=f"{numerator.source}|{denominator.source}",
            basis=basis,
            reason=reason,
            caveats=caveats,
        )
    return ProvenancedValue(
        value=numerator.value / denominator.value,
        inputs=numerator.inputs + denominator.inputs,
        source=f"{numerator.source}|{denominator.source}",
        basis=basis,
        caveats=caveats,
    )


def _sum_optional(
    values: list[ProvenancedValue],
    zero_if_missing: set[int],
    basis: str,
    field_name: str,
    data_gaps: list[str],
) -> ProvenancedValue:
    """Sum values; indices in ``zero_if_missing`` default to 0 when absent (concept
    genuinely not reported by this filer), all others are required inputs."""
    total = 0.0
    inputs: list[str] = []
    sources: list[str] = []
    caveat_groups: list[list[str]] = []
    missing_required: list[str] = []
    for index, item in enumerate(values):
        caveat_groups.append(item.caveats)
        if item.value is None:
            if index in zero_if_missing:
                inputs.append(f"component[{index}]=0 (missing: {item.reason})")
                continue
            missing_required.append(item.reason or "missing")
            continue
        total += item.value
        inputs.extend(item.inputs)
        sources.append(item.source)
    caveats = _merge_caveats(*caveat_groups)
    if missing_required:
        reason = f"missing required inputs: {', '.join(missing_required)}"
        data_gaps.append(f"{field_name}: {reason}")
        return ProvenancedValue(
            value=None, inputs=inputs, source="|".join(sources) or "sec_companyfacts", basis=basis, reason=reason,
            caveats=caveats,
        )
    return ProvenancedValue(
        value=total, inputs=inputs, source="|".join(sources) or "sec_companyfacts", basis=basis, caveats=caveats,
    )


# ---------------------------------------------------------------------------
# Shares / market cap
# ---------------------------------------------------------------------------

def _resolve_shares(fundamentals: ValuationFundamentals | None, data_gaps: list[str]) -> ProvenancedValue:
    if fundamentals is None:
        data_gaps.append("shares_used: no SEC companyfacts data")
        return ProvenancedValue(value=None, source="sec_companyfacts", reason="no_fundamentals")

    diluted = fundamentals.concepts.get("diluted_weighted_avg_shares")
    if diluted and diluted.datapoints:
        dp = diluted.datapoints[0]
        caveat = _stale_caveat(fundamentals, "diluted_weighted_avg_shares")
        return ProvenancedValue(
            value=dp.value,
            inputs=[dp.end.isoformat()],
            source=f"sec_companyfacts:{diluted.tag}",
            basis="diluted_weighted_average_shares_latest_quarter",
            caveats=[caveat] if caveat else [],
        )

    if fundamentals.cover_page_shares is not None:
        class_count = fundamentals.cover_page_meta.get("class_count")
        accn = fundamentals.cover_page_meta.get("accn")
        return ProvenancedValue(
            value=fundamentals.cover_page_shares,
            inputs=[entry.as_of.isoformat() for entry in fundamentals.cover_page_share_classes],
            source=(
                f"sec_companyfacts:{fundamentals.cover_page_meta.get('tag', 'dei:EntityCommonStockSharesOutstanding')}"
                f" (summed across {class_count} class(es), accn {accn})"
            ),
            basis="cover_page_shares_summed_across_classes",
        )

    basic = fundamentals.concepts.get("basic_weighted_avg_shares")
    if basic and basic.datapoints:
        dp = basic.datapoints[0]
        caveat = _stale_caveat(fundamentals, "basic_weighted_avg_shares")
        return ProvenancedValue(
            value=dp.value,
            inputs=[dp.end.isoformat()],
            source=f"sec_companyfacts:{basic.tag}",
            basis="basic_weighted_average_shares_latest_quarter",
            caveats=[caveat] if caveat else [],
        )

    reason = "no diluted, cover-page, or basic share count available"
    data_gaps.append(f"shares_used: {reason}")
    return ProvenancedValue(value=None, source="sec_companyfacts", reason=reason)


def _resolve_market_cap(
    shares_used: ProvenancedValue, prices: list[PriceBar], data_gaps: list[str]
) -> ProvenancedValue:
    if not prices:
        data_gaps.append("market_cap: no price bars available")
        return ProvenancedValue(value=None, source="price_lane", reason="no_price_bars")
    latest_bar = max(prices, key=lambda bar: bar.trading_date)
    if shares_used.value is None:
        data_gaps.append("market_cap: shares_used unavailable")
        return ProvenancedValue(
            value=None,
            inputs=[f"close={latest_bar.close}@{latest_bar.trading_date.isoformat()}({latest_bar.source})"],
            source=f"{latest_bar.source}|sec_companyfacts",
            reason="missing_share_count",
        )
    return ProvenancedValue(
        value=latest_bar.close * shares_used.value,
        inputs=[
            f"close={latest_bar.close}@{latest_bar.trading_date.isoformat()}({latest_bar.source})",
            f"shares_used={shares_used.value}",
        ],
        source=f"{latest_bar.source}|sec_companyfacts",
        basis="latest_close_times_shares_used",
        caveats=shares_used.caveats,
    )


# ---------------------------------------------------------------------------
# Beta — in-house OLS regression of daily security returns on benchmark returns
# ---------------------------------------------------------------------------

def compute_beta(prices: list[PriceBar], benchmark_prices: list[PriceBar]) -> BetaEstimate:
    """OLS regression of daily security returns on benchmark returns.

    Alignment is by trading DATE, not by list position: returns are computed only
    across consecutive dates that both the security and the benchmark actually have a
    bar for. Computing each series' own returns independently first (using each
    series' own previous bar) and only intersecting afterward is wrong whenever the two
    calendars diverge even briefly — e.g. the security has a bar on a date the
    benchmark is missing (or vice versa) — because the two series would then each be
    measuring returns over different, offsetting date spans while being paired as if
    they covered the same interval. Restricting to shared close-dates *before* taking
    any difference avoids that entirely.
    """
    security_closes = _closes_by_date(prices)
    benchmark_closes = _closes_by_date(benchmark_prices)
    common_dates = sorted(set(security_closes) & set(benchmark_closes))

    x: list[float] = []
    y: list[float] = []
    for previous_date, current_date in zip(common_dates, common_dates[1:]):
        prev_security = security_closes[previous_date]
        prev_benchmark = benchmark_closes[previous_date]
        if not prev_security or not prev_benchmark:
            continue
        y.append((security_closes[current_date] - prev_security) / prev_security)
        x.append((benchmark_closes[current_date] - prev_benchmark) / prev_benchmark)

    n = len(x)
    if n < _MIN_BETA_OBSERVATIONS:
        return BetaEstimate(
            beta=None, observations=n, r_squared=None, reliable=False,
            reason=f"insufficient_paired_daily_returns (n={n}, need >= {_MIN_BETA_OBSERVATIONS})",
        )

    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    var_y = sum((yi - mean_y) ** 2 for yi in y)

    if var_x == 0:
        return BetaEstimate(
            beta=None, observations=n, r_squared=None, reliable=False,
            reason="zero_benchmark_return_variance",
        )

    beta = cov / var_x
    if var_y == 0:
        r_squared = 0.0
    else:
        corr = cov / ((var_x ** 0.5) * (var_y ** 0.5))
        r_squared = corr ** 2

    reliability_notes: list[str] = []
    if n < _MIN_RELIABLE_OBSERVATIONS:
        reliability_notes.append(f"only {n} paired daily observations, need >= {_MIN_RELIABLE_OBSERVATIONS}")
    if r_squared < _MIN_RELIABLE_R_SQUARED:
        reliability_notes.append(f"r_squared {r_squared:.4f} below reliability threshold {_MIN_RELIABLE_R_SQUARED}")

    reliable = not reliability_notes
    reason = None
    if reliability_notes:
        reason = "unreliable beta estimate: " + "; ".join(reliability_notes)
    elif n < 60:
        reason = f"rough estimate: only {n} daily observations (~{n} trading days), treat with low confidence"

    return BetaEstimate(beta=beta, observations=n, r_squared=r_squared, reason=reason, reliable=reliable)


def _closes_by_date(bars: list[PriceBar]) -> dict[date, float]:
    """Latest close per trading date (guards against accidental duplicate bars for the
    same date from merged/cached fetches — last one after date-sort wins)."""
    ordered = sorted(bars, key=lambda bar: bar.trading_date)
    closes: dict[date, float] = {}
    for bar in ordered:
        if bar.close:
            closes[bar.trading_date] = bar.close
    return closes
