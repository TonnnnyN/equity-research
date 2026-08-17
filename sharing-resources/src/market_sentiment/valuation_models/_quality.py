"""Group 4 — quality and risk: Piotroski F-Score, Altman Z''-Score, Beneish M-Score.

Pure formulas, no assumptions — the most robust group per the task spec. Every
formula below was verified against a reputable source before implementation (cited
in each function's docstring); none is trusted from memory alone.
"""
from __future__ import annotations

from market_sentiment.models import Layer, ValuationDerived, ValuationFundamentals
from market_sentiment.valuation_models._series import prior_year_pair, rolling_ttm_series, instant_series
from market_sentiment.valuation_models._types import GROUP_QUALITY_AND_RISK, ModelResult, STATUS_OK, STATUS_SKIPPED


def _skip(model: str, reason: str) -> ModelResult:
    return ModelResult(model=model, group=GROUP_QUALITY_AND_RISK, status=STATUS_SKIPPED, skip_reason=reason)


def piotroski_f_score(fundamentals: ValuationFundamentals | None) -> ModelResult:
    """Piotroski F-Score (0-9). Each of 9 binary criteria scores 1 point; the total
    reported is ``score / max_score`` where ``max_score`` is REDUCED (not padded to
    9) when a criterion's two-year comparison cannot be computed from available data
    — a partial score out of a stated denominator, never a fabricated point.

    Criteria and source: Piotroski, J. D. (2000), "Value Investing: The Use of
    Historical Financial Statement Information to Separate Winners from Losers,"
    Journal of Accounting Research 38 (Supplement): 1-41; cross-verified against
    Wikipedia's "Piotroski F-score" summary (both agree on all 9 criteria):
    ROA > 0; CFO > 0; delta-ROA > 0; CFO > net income (accrual quality); delta
    (long-term debt / total assets) < 0; delta current ratio > 0; no new shares
    issued; delta gross margin > 0; delta asset turnover > 0.

    Deliberate simplification: uses point-in-time total_assets rather than the
    academic average of beginning/ending total assets for the period — documented
    here rather than silently applied, since only point-in-time balance-sheet
    snapshots are available from this data layer.
    """
    if fundamentals is None:
        return _skip("piotroski_f_score", "no ValuationFundamentals available")

    revenue = rolling_ttm_series(fundamentals.concepts.get("revenue"))
    gross_profit = rolling_ttm_series(fundamentals.concepts.get("gross_profit"))
    net_income = rolling_ttm_series(fundamentals.concepts.get("net_income"))
    ocf = rolling_ttm_series(fundamentals.concepts.get("operating_cashflow"))

    def cur_prior_ttm(series: list[tuple]) -> tuple[float | None, float | None]:
        # windows[0] = current TTM, windows[4] = TTM ending ~4 quarters earlier (a
        # full year earlier), by construction of rolling_ttm_series' sliding window.
        if len(series) > 4:
            return series[0][1], series[4][1]
        return (series[0][1], None) if series else (None, None)

    rev_c, rev_p = cur_prior_ttm(revenue)
    gp_c, gp_p = cur_prior_ttm(gross_profit)
    ni_c, ni_p = cur_prior_ttm(net_income)
    ocf_c, ocf_p = cur_prior_ttm(ocf)

    def instant(concept: str) -> list[tuple]:
        return instant_series(fundamentals.concepts.get(concept))

    ta_c, ta_p = prior_year_pair(instant("total_assets"))
    ltd_c, ltd_p = prior_year_pair(instant("long_term_debt"))
    ca_c, ca_p = prior_year_pair(instant("total_current_assets"))
    cl_c, cl_p = prior_year_pair(instant("total_current_liabilities"))
    shares_c, shares_p = prior_year_pair(
        instant("diluted_weighted_avg_shares") or instant("basic_weighted_avg_shares")
    )

    def safe_div(a: float | None, b: float | None) -> float | None:
        if a is None or b in (None, 0):
            return None
        return a / b

    roa_c, roa_p = safe_div(ni_c, ta_c), safe_div(ni_p, ta_p)
    lev_c, lev_p = safe_div(ltd_c, ta_c), safe_div(ltd_p, ta_p)
    cr_c, cr_p = safe_div(ca_c, cl_c), safe_div(ca_p, cl_p)
    gm_c, gm_p = safe_div(gp_c, rev_c), safe_div(gp_p, rev_p)
    at_c, at_p = safe_div(rev_c, ta_c), safe_div(rev_p, ta_p)

    def gt(a: float | None, b: float | None) -> bool | None:
        return None if a is None or b is None else a > b

    def lt(a: float | None, b: float | None) -> bool | None:
        return None if a is None or b is None else a < b

    def le(a: float | None, b: float | None) -> bool | None:
        return None if a is None or b is None else a <= b

    criteria: dict[str, bool | None] = {
        "roa_positive": (roa_c > 0) if roa_c is not None else None,
        "cfo_positive": (ocf_c > 0) if ocf_c is not None else None,
        "roa_improved": gt(roa_c, roa_p),
        "accruals_quality_cfo_exceeds_net_income": gt(ocf_c, ni_c),
        "leverage_decreased": lt(lev_c, lev_p),
        "current_ratio_improved": gt(cr_c, cr_p),
        "no_new_shares_issued": le(shares_c, shares_p),
        "gross_margin_improved": gt(gm_c, gm_p),
        "asset_turnover_improved": gt(at_c, at_p),
    }

    scored = {k: v for k, v in criteria.items() if v is not None}
    if not scored:
        return _skip(
            "piotroski_f_score",
            "insufficient two-year history for every criterion (need >= 8 contiguous TTM quarters and "
            "~1-year-apart balance-sheet snapshots)",
        )

    score = sum(1 for v in scored.values() if v)
    max_score = len(scored)
    missing = [k for k, v in criteria.items() if v is None]

    caveats = [
        "Uses point-in-time total_assets, not the average of beginning/ending total assets used in some "
        "academic implementations — a documented simplification, not a hidden one.",
        "If max_score < 9, the denominator is reduced, not padded — compare score/max_score, never treat a "
        "reduced score as if it were out of 9.",
    ]
    if missing:
        caveats.append(f"criteria unavailable due to insufficient history: {missing}")

    return ModelResult(
        model="piotroski_f_score",
        group=GROUP_QUALITY_AND_RISK,
        status=STATUS_OK,
        assumptions={"source": "Piotroski (2000), Journal of Accounting Research 38(Supplement):1-41"},
        outputs={"score": score, "max_score": max_score, "criteria": criteria},
        caveats=caveats,
    )


_Z_DOUBLE_PRIME_THRESHOLDS = {"distress": 1.1, "safe": 2.6}


def altman_z_score(
    derived: ValuationDerived, fundamentals: ValuationFundamentals | None, layer: Layer | None = None
) -> ModelResult:
    """Altman Z-Score, using the Z'' (Z-double-prime) non-manufacturer variant:
    Z'' = 6.56*X1 + 3.26*X2 + 6.72*X3 + 1.05*X4, where X1 = working_capital /
    total_assets, X2 = retained_earnings / total_assets, X3 = EBIT / total_assets
    (EBIT proxied by ttm_operating_income), X4 = book value of equity / total
    liabilities. Thresholds: distress < 1.1, grey 1.1-2.6, safe > 2.6.

    Source: Altman, E. I., Hartzell, J., & Peck, M. (1995), "Emerging Markets
    Corporate Bonds: A Scoring System," Salomon Brothers; cross-verified against
    Wikipedia's "Altman Z-score" and CreditGuru's summary of the Z''-Score model
    (both give identical coefficients, variable definitions, and thresholds).

    Z'' is used DELIBERATELY rather than the classic manufacturer Z-score (which
    adds a sales/total-assets term, X5) — this watchlist is software / AI
    applications / compute / pharma / utilities, i.e. non-manufacturing filers for
    which asset turnover is not comparable across business models (task spec:
    choosing the wrong variant produces confidently wrong distress readings for
    every software name).
    """
    total_assets, ta_reason = _latest(fundamentals, "total_assets")
    total_liabilities, tl_reason = _latest(fundamentals, "total_liabilities")
    retained_earnings, re_reason = _latest(fundamentals, "retained_earnings")
    working_capital = derived.working_capital.value
    ebit = derived.ttm_operating_income.value
    book_equity = derived.book_value.value

    missing = []
    if total_assets is None:
        missing.append(f"total_assets ({ta_reason})")
    if total_liabilities is None:
        missing.append(f"total_liabilities ({tl_reason})")
    if retained_earnings is None:
        missing.append(f"retained_earnings ({re_reason})")
    if working_capital is None:
        missing.append(f"working_capital ({derived.working_capital.reason})")
    if ebit is None:
        missing.append(f"ttm_operating_income/EBIT proxy ({derived.ttm_operating_income.reason})")
    if book_equity is None:
        missing.append(f"book_value/stockholders_equity ({derived.book_value.reason})")
    if missing:
        return _skip("altman_z_score", "missing required inputs: " + "; ".join(missing))
    if total_assets == 0 or total_liabilities == 0:
        return _skip("altman_z_score", "total_assets or total_liabilities is 0 — Z'' undefined")

    x1 = working_capital / total_assets
    x2 = retained_earnings / total_assets
    x3 = ebit / total_assets
    x4 = book_equity / total_liabilities
    z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4

    if z < _Z_DOUBLE_PRIME_THRESHOLDS["distress"]:
        zone = "distress"
    elif z < _Z_DOUBLE_PRIME_THRESHOLDS["safe"]:
        zone = "grey"
    else:
        zone = "safe"

    caveats = [
        "Z'' (non-manufacturer) variant used deliberately for this non-manufacturing watchlist — see docstring "
        "for source and rationale. Do not compare this number to a classic manufacturer Z-score threshold.",
    ]
    if layer == Layer.UTILITIES:
        caveats.append(
            "Z-scores are poorly calibrated for capital-intensive, regulated utilities with structurally high, "
            "regulator-sanctioned leverage — treat this zone classification with extra caution for this layer."
        )

    return ModelResult(
        model="altman_z_score",
        group=GROUP_QUALITY_AND_RISK,
        status=STATUS_OK,
        assumptions={
            "variant": "Z'' (Z-double-prime, non-manufacturer)",
            "source": "Altman, Hartzell & Peck (1995), Salomon Brothers Emerging Markets scoring system",
            "thresholds": {"distress": "<1.1", "grey": "1.1-2.6", "safe": ">2.6"},
        },
        outputs={
            "z_score": z,
            "zone": zone,
            "x1_working_capital_to_assets": x1,
            "x2_retained_earnings_to_assets": x2,
            "x3_ebit_to_assets": x3,
            "x4_book_equity_to_liabilities": x4,
        },
        caveats=caveats,
    )


# ---------------------------------------------------------------------------
# Beneish M-Score
# ---------------------------------------------------------------------------

# Raw SEC concepts each of the 8 Beneish variables needs that this data layer does
# NOT currently extract at all (see sources/sec.py's _FLOW_CONCEPT_TAGS /
# _INSTANT_CONCEPT_TAGS — no receivables, SG&A, or gross PP&E tag is present in
# either candidate-tag table). Per the task spec, a missing input here must skip the
# model rather than substitute a default — this is an earnings-manipulation screen,
# and a silently-defaulted input could hide the exact thing it exists to catch.
_BENEISH_UNAVAILABLE_CONCEPTS = {
    "accounts_receivable": "DSRI (Days' Sales in Receivables Index)",
    "sga_expense": "SGAI (SG&A Expense Index)",
    "gross_ppe": "AQI (Asset Quality Index) and DEPI (Depreciation Index)",
}


def beneish_m_score(fundamentals: ValuationFundamentals | None) -> ModelResult:
    """8-ratio earnings-manipulation screen (Beneish, 1999): M = -4.84 + 0.920*DSRI +
    0.528*GMI + 0.404*AQI + 0.892*SGI + 0.115*DEPI - 0.172*SGAI - 0.327*LVGI +
    4.679*TATA. M > -1.78 flags a heightened probability of manipulation.

    Source: Beneish, M. D. (1999), "The Detection of Earnings Manipulation,"
    Financial Analysts Journal 55(5): 24-36; cross-verified against Wikipedia's
    "Beneish M-score" page (identical formula, coefficients, and -1.78 cutoff).

    This project's SEC extraction (sources/sec.py) does not currently pull accounts
    receivable, SG&A expense, or gross PP&E — three of the eight required inputs —
    so per the task spec this model is skipped rather than computed with substituted
    defaults. The computation is implemented below so it activates automatically the
    moment those concepts are added to the data layer.
    """
    if fundamentals is None:
        return _skip("beneish_m_score", "no ValuationFundamentals available")

    missing_concepts = [
        f"{concept} (needed for {label})"
        for concept, label in _BENEISH_UNAVAILABLE_CONCEPTS.items()
        if concept not in fundamentals.concepts
    ]
    if missing_concepts:
        return _skip(
            "beneish_m_score",
            "required inputs not extracted by the SEC valuation data layer: " + "; ".join(missing_concepts) + ". "
            "Per spec, Beneish is skipped rather than substituting defaults for a manipulation-detection screen.",
        )

    # --- reachable only once the data layer extracts the concepts above ---
    receivables = rolling_or_instant(fundamentals, "accounts_receivable")
    revenue = rolling_ttm_series(fundamentals.concepts.get("revenue"))
    gross_profit = rolling_ttm_series(fundamentals.concepts.get("gross_profit"))
    sga = rolling_ttm_series(fundamentals.concepts.get("sga_expense"))
    da = rolling_ttm_series(fundamentals.concepts.get("depreciation_amortization"))
    ppe = instant_series(fundamentals.concepts.get("gross_ppe"))
    total_assets = instant_series(fundamentals.concepts.get("total_assets"))
    current_assets = instant_series(fundamentals.concepts.get("total_current_assets"))
    current_liabilities = instant_series(fundamentals.concepts.get("total_current_liabilities"))
    long_term_debt = instant_series(fundamentals.concepts.get("long_term_debt"))
    net_income = rolling_ttm_series(fundamentals.concepts.get("net_income"))
    ocf = rolling_ttm_series(fundamentals.concepts.get("operating_cashflow"))
    securities = instant_series(fundamentals.concepts.get("short_term_investments"))

    rec_c, rec_p = prior_year_pair(receivables)
    rev_c, rev_p = prior_year_pair(revenue)
    gp_c, gp_p = prior_year_pair(gross_profit)
    sga_c, sga_p = prior_year_pair(sga)
    da_c, da_p = prior_year_pair(da)
    ppe_c, ppe_p = prior_year_pair(ppe)
    ta_c, ta_p = prior_year_pair(total_assets)
    ca_c, ca_p = prior_year_pair(current_assets)
    cl_c, cl_p = prior_year_pair(current_liabilities)
    ltd_c, ltd_p = prior_year_pair(long_term_debt)
    ni_c, ni_p = prior_year_pair(net_income)
    ocf_c, ocf_p = prior_year_pair(ocf)
    sec_c, sec_p = prior_year_pair(securities)

    required = [rec_c, rec_p, rev_c, rev_p, gp_c, gp_p, sga_c, sga_p, da_c, da_p, ppe_c, ppe_p, ta_c, ta_p, ca_c, ca_p, cl_c, cl_p, ltd_c, ltd_p, ni_c, ni_p, ocf_c, ocf_p, sec_c, sec_p]
    if any(v is None for v in required):
        return _skip("beneish_m_score", "insufficient two-year history for one or more of the 8 Beneish inputs")

    dsri = (rec_c / rev_c) / (rec_p / rev_p)
    # GMI = prior gross margin / current gross margin, where gross margin = (Sales -
    # COGS)/Sales = GrossProfit/Sales (COGS = Sales - GrossProfit, so Sales-COGS ==
    # GrossProfit — do not substitute COGS/Sales here, that is the complement, not
    # the margin).
    gmi = (gp_p / rev_p) / (gp_c / rev_c)
    aqi = (1 - (ca_c + ppe_c + sec_c) / ta_c) / (1 - (ca_p + ppe_p + sec_p) / ta_p)
    sgi = rev_c / rev_p
    depi = (da_p / (ppe_p + da_p)) / (da_c / (ppe_c + da_c))
    sgai = (sga_c / rev_c) / (sga_p / rev_p)
    lvgi = ((cl_c + ltd_c) / ta_c) / ((cl_p + ltd_p) / ta_p)
    tata = (ni_c - ocf_c) / ta_c

    m = -4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi + 0.115 * depi - 0.172 * sgai - 0.327 * lvgi + 4.679 * tata

    return ModelResult(
        model="beneish_m_score",
        group=GROUP_QUALITY_AND_RISK,
        status=STATUS_OK,
        assumptions={"source": "Beneish (1999), Financial Analysts Journal 55(5):24-36", "manipulation_cutoff": -1.78},
        outputs={
            "m_score": m,
            "flag_likely_manipulator": m > -1.78,
            "dsri": dsri, "gmi": gmi, "aqi": aqi, "sgi": sgi, "depi": depi, "sgai": sgai, "lvgi": lvgi, "tata": tata,
        },
        caveats=["A screen, not a verdict — M > -1.78 means heightened statistical likelihood of earnings manipulation in the original sample, not proof."],
    )


def rolling_or_instant(fundamentals: ValuationFundamentals, concept: str) -> list[tuple]:
    """accounts_receivable would be an instant (balance-sheet) concept if the data
    layer ever extracts it — kept as a separate seam so this stays correct without
    guessing at extraction details that don't exist yet."""
    return instant_series(fundamentals.concepts.get(concept))


def _latest(fundamentals: ValuationFundamentals | None, concept: str) -> tuple[float | None, str | None]:
    if fundamentals is None:
        return None, "no ValuationFundamentals available"
    history = fundamentals.concepts.get(concept)
    if history is None or not history.datapoints:
        return None, f"{concept}: no data from SEC companyfacts"
    dp = sorted(history.datapoints, key=lambda p: p.end, reverse=True)[0]
    return dp.value, None
