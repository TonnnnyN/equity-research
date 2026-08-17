from __future__ import annotations

from pathlib import Path
from datetime import date, datetime, timezone

from market_sentiment.http import HttpClient
from market_sentiment.models import (
    ConceptDatapoint,
    ConceptHistory,
    FilingSummaryCacheRow,
    FundamentalSnapshot,
    OfficialEvent,
    ShareClassEntry,
    SourceStatus,
    ValuationFundamentals,
)
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage


# Forms accepted for extraction, including amendments (10-K/A, 10-Q/A) so a restated
# datapoint can win the dedupe-by-newest-filed pass instead of being silently dropped.
_ACCEPTED_FORMS = {"10-K", "10-Q", "20-F", "6-K", "10-K/A", "10-Q/A"}

# Flow (duration) concepts: canonical name -> ordered candidate us-gaap tags.
# require_duration=True extraction keeps only entries shaped like a single fiscal quarter.
_FLOW_CONCEPT_TAGS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt", "InterestIncomeExpenseNet"],
    "income_tax_expense": ["IncomeTaxExpenseBenefit"],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
    ],
    "operating_cashflow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PropertyPlantAndEquipmentAdditions",
    ],
    "share_based_compensation": [
        "ShareBasedCompensation",
        "AllocatedShareBasedCompensationExpense",
    ],
    "diluted_weighted_avg_shares": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted",
    ],
    "basic_weighted_avg_shares": ["WeightedAverageNumberOfSharesOutstandingBasic"],
    # Beneish M-Score's SGAI input. Only the single combined tag is listed here — the
    # split-selling-vs-G&A fallback (some filers never report a combined figure) is
    # handled separately by _extract_sga_split_tag_history, NOT by adding
    # GeneralAndAdministrativeExpense / SellingAndMarketingExpense as ordinary fallback
    # candidates below. Falling back to either split tag ALONE, the way every other
    # concept's ordered-candidate-list fallback works, would silently redefine "SG&A"
    # as merely G&A or merely Selling for that filer, corrupting the SGAI year-over-year
    # ratio. See _extract_sga_split_tag_history's docstring for the deliberate,
    # documented sum-only-when-both-tags-cover-the-same-period behaviour.
    "sga_expense": ["SellingGeneralAndAdministrativeExpense"],
}

# Flow concepts that are period-additive (a dollar total that genuinely sums across time),
# so year-to-date cumulative facts (H1, 9M, FY) can be subtracted pairwise to derive the
# discrete quarter that a filer never reported standalone (see _quarterize_duration_entries).
# Weighted-average share counts are duration facts too but are NOT additive across periods
# (a 6-month average is not "Q1 average + Q2 average"), so they are deliberately excluded
# and keep going through the simple quarter-shaped-only filter.
_ADDITIVE_FLOW_CONCEPTS: frozenset[str] = frozenset(
    {
        "revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "interest_expense",
        "income_tax_expense",
        "depreciation_amortization",
        "operating_cashflow",
        "capex",
        "share_based_compensation",
        "sga_expense",
    }
)

# Instant (point-in-time) concepts: canonical name -> ordered candidate us-gaap tags.
#
# short_term_investments / long_term_investments: NEVER sum two of these tags together for
# the same period — they can describe the same underlying balance under different filer
# conventions. Extraction always picks the first candidate tag that has data; see
# _extract_concept_history's "first match wins" contract.
#
# non_operating_assets is a DELIBERATELY separate concept from long_term_investments even
# though both list LongTermInvestments as a candidate tag: for filers like Zoom, the
# LongTermInvestments balance is not marketable securities at all but a strategic/venture
# equity-stake bucket (e.g. Zoom's Anthropic position). valuation.py's total_liquid_assets
# never includes long_term_investments or non_operating_assets — only cash and *current*
# marketable securities count as liquid; illiquid strategic stakes are reported separately
# so a later model layer can apply a haircut instead of treating them as a cash-like floor.
_INSTANT_CONCEPT_TAGS: dict[str, list[str]] = {
    "cash_and_equivalents": ["CashAndCashEquivalentsAtCarryingValue"],
    "short_term_investments": [
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "MarketableSecuritiesCurrent",
        "ShortTermInvestments",
        "AvailableForSaleSecuritiesCurrent",
        "OtherShortTermInvestments",
    ],
    "long_term_investments": [
        "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
        "MarketableSecuritiesNoncurrent",
        "LongTermInvestments",
    ],
    "non_operating_assets": [
        "LongTermInvestments",
        "AlternativeInvestment",
        "EquityMethodInvestments",
        "EquityMethodInvestmentsFairValueDisclosure",
        "EquitySecuritiesFvNiCurrentAndNoncurrent",
    ],
    "total_assets": ["Assets"],
    "total_current_assets": ["AssetsCurrent"],
    "total_current_liabilities": ["LiabilitiesCurrent"],
    "total_liabilities": ["Liabilities"],
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "long_term_debt": [
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebtNoncurrent",
        "LongTermDebt",
    ],
    "finance_lease_obligations": [
        "FinanceLeaseLiabilityNoncurrent",
        "FinanceLeaseLiability",
        "CapitalLeaseObligationsNoncurrent",
    ],
    # Beneish M-Score's DSRI input.
    "accounts_receivable": [
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
        "AccountsReceivableGrossCurrent",
        "AccountsReceivableNet",
    ],
    # Beneish M-Score's AQI/DEPI input. AQI and DEPI are both defined in terms of GROSS
    # PP&E (DEPI in particular divides accumulated depreciation into gross PP&E + prior
    # depreciation; using net PP&E instead double-counts accumulated depreciation and
    # skews both ratios). PropertyPlantAndEquipmentNet is listed as a fallback ONLY
    # because some filers never tag a gross figure at all -- when that fallback fires,
    # _build_valuation_fundamentals records a note on ValuationFundamentals.notes (and
    # the fallback is visible directly on ConceptHistory.tag) so a consumer can see
    # gross_ppe is actually net for that ticker, rather than silently trusting AQI/DEPI.
    "gross_ppe": [
        "PropertyPlantAndEquipmentGross",
        "PropertyPlantAndEquipmentNet",
    ],
}

# Split-tag fallback pair for sga_expense — see _extract_sga_split_tag_history.
_SGA_SPLIT_TAGS: tuple[str, str] = ("GeneralAndAdministrativeExpense", "SellingAndMarketingExpense")

_DEI_COVER_SHARES_TAG = "EntityCommonStockSharesOutstanding"
_QUARTER_MIN_DAYS = 60
_QUARTER_MAX_DAYS = 100
_MAX_HISTORY_ITEMS = 20

# Recency window for multi-tag concept resolution: a candidate tag is "fresh" only if
# its own most recent datapoint falls within this many days of the filer's anchor
# period (see _determine_anchor_period). 548 days (~18 months) is deliberately wider
# than a single fiscal year: a 10-Q filer reports every ~91 days, but an annual-only
# 20-F foreign private issuer reports a given instant tag only once a year and gets up
# to ~4 months of statutory filing lag after fiscal year-end, so 12mo + ~6mo of buffer
# comfortably covers a currently-used tag on any normal filer cadence (including a
# fiscal-year change) without being so wide that it would still accept a tag genuinely
# abandoned by a taxonomy migration -- those gaps run to multiple YEARS in practice
# (e.g. Apple's AvailableForSaleSecuritiesDebtSecuritiesCurrent, last reported
# 2011-03-26, sits ~15 years outside this window).
_STALE_TAG_WINDOW_DAYS = 548

# Substring baked into ConceptHistory.tag when _select_tag_order had to fall back to a
# candidate outside the freshness window -- lets _build_valuation_fundamentals (and any
# other consumer) detect a stale read by string containment, without a separate
# boolean field threading through every extraction function.
_STALE_FALLBACK_MARKER = "STALE FALLBACK"


class SecClient:
    ticker_map_url = "https://www.sec.gov/files/company_tickers_exchange.json"
    submissions_url = "https://data.sec.gov/submissions/CIK{cik}.json"
    companyfacts_url = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    filing_index = "https://www.sec.gov/Archives/edgar/data/{cik_numeric}/{accession}/{primary_document}"

    def __init__(self, http_client: HttpClient, storage: Storage) -> None:
        self._http = http_client
        self._storage = storage
        self._ticker_map_cache: dict[str, str] | None = None
        # Caches the raw companyfacts payload per (ticker, run_date) so fetch_company_facts
        # and fetch_valuation_fundamentals share ONE HTTP request instead of two.
        self._companyfacts_payload_cache: dict[
            tuple[str, date],
            tuple[str | None, dict | None, str | None, Path | None, str | None],
        ] = {}

    def _load_ticker_map(self, run_date: date) -> dict[str, str]:
        if self._ticker_map_cache is not None:
            return self._ticker_map_cache
        response = self._http.get(self.ticker_map_url)
        payload = response.json()
        self._storage.write_raw_json(run_date, "sec", "company_tickers_exchange", payload)
        data = payload.get("data", [])
        mapping = {
            str(item[2]).upper(): str(item[0]).zfill(10)
            for item in data
            if len(item) > 2
        }
        self._ticker_map_cache = mapping
        return mapping

    def cik_for_ticker(self, ticker: str, run_date: date) -> str | None:
        return self._load_ticker_map(run_date).get(ticker.upper())

    def fetch_recent_events(self, ticker: str, run_date: date) -> SourcePayload[list[OfficialEvent]]:
        ingested_at = datetime.now(timezone.utc)
        try:
            cik = self.cik_for_ticker(ticker, run_date)
        except RuntimeError as exc:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="sec",
                    success=False,
                    partial=True,
                    message=f"Failed to load SEC ticker map for {ticker}: {exc}",
                    ingested_at=ingested_at,
                ),
            )
        if not cik:
            return SourcePayload(
                data=[],
                status=SourceStatus(
                    source="sec",
                    success=False,
                    partial=True,
                    message=f"No CIK mapping for {ticker}",
                    ingested_at=ingested_at,
                ),
            )

        response = self._http.get(self.submissions_url.format(cik=cik))
        safe_url = getattr(response, "safe_url", response.url)
        payload = response.json()
        raw_path = self._storage.write_raw_json(run_date, "sec", f"{ticker.lower()}_submissions", payload)
        recent = payload.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        filing_dates = recent.get("filingDate", [])
        accession_numbers = recent.get("accessionNumber", [])
        acceptance_datetimes = recent.get("acceptanceDateTime", [])
        primary_documents = recent.get("primaryDocument", [])
        descriptions = recent.get("primaryDocDescription", [])
        items = list(zip(
            forms,
            filing_dates,
            accession_numbers,
            acceptance_datetimes,
            primary_documents,
            descriptions,
            strict=False,
        ))
        events: list[OfficialEvent] = []
        cik_numeric = str(int(cik))
        for form_type, filing_date, accession_number, acceptance_datetime, primary_document, description in items:
            accession_no_dashes = accession_number.replace("-", "")
            events.append(
                OfficialEvent(
                    ticker=ticker,
                    event_time=datetime.strptime(filing_date, "%Y-%m-%d"),
                    form_type=form_type,
                    title=(description or f"{ticker} filed {form_type}").strip(),
                    url=self.filing_index.format(
                        cik_numeric=cik_numeric,
                        accession=accession_no_dashes,
                        primary_document=primary_document,
                    ),
                    source="sec",
                    accepted_at=_parse_acceptance_datetime(acceptance_datetime),
                    ingested_at=ingested_at,
                )
            )
        cached_rows = self._storage.get_filing_summaries_for_ticker(ticker)
        cached_keys = {row.accession_number for row in cached_rows}
        new_rows = [
            FilingSummaryCacheRow(
                cik=cik,
                accession_number=accession_number,
                ticker=ticker,
                form_type=form_type,
                filed_at=datetime.strptime(filing_date, "%Y-%m-%d").replace(tzinfo=timezone.utc),
                period_end=None,
                summary=(description or f"{ticker} filed {form_type}").strip(),
                sentiment="unknown",
                key_metrics_json="{}",
                ingested_at=ingested_at,
            )
            for form_type, filing_date, accession_number, _, _, description in items
            if accession_number not in cached_keys
        ]
        self._storage.upsert_filing_summary_cache(new_rows)
        hit_count = len(events) - len(new_rows)
        status = SourceStatus(
            source="sec",
            success=bool(events),
            partial=not bool(events),
            message=f"ok; cache_hit={hit_count}/{len(events)}" if events else "empty recent filings",
            payload_path=str(raw_path),
            source_url=safe_url,
            ingested_at=ingested_at,
            event_start=min((event.event_time for event in events), default=None),
            event_end=max((event.event_time for event in events), default=None),
        )
        return SourcePayload(data=events, status=status, raw_path=raw_path)

    def _get_companyfacts_payload(
        self, ticker: str, run_date: date
    ) -> tuple[str | None, dict | None, str | None, Path | None, str | None]:
        """Fetch (or reuse a cached) raw companyfacts payload for one ticker/run_date.

        Returns (cik, payload, safe_url, raw_path, error_message). Exactly ONE HTTP
        request is made per (ticker, run_date) even though both fetch_company_facts
        and fetch_valuation_fundamentals need the same payload — SEC companyfacts
        already returns every tag in one call, so a second call would be wasteful
        and edges toward the 10 req/s rate limit for no reason.
        """
        cache_key = (ticker.upper(), run_date)
        if cache_key in self._companyfacts_payload_cache:
            return self._companyfacts_payload_cache[cache_key]

        try:
            cik = self.cik_for_ticker(ticker, run_date)
        except RuntimeError as exc:
            result = (None, None, None, None, f"Failed to load SEC ticker map for {ticker}: {exc}")
            self._companyfacts_payload_cache[cache_key] = result
            return result
        if not cik:
            result = (None, None, None, None, f"No CIK mapping for {ticker}")
            self._companyfacts_payload_cache[cache_key] = result
            return result

        response = self._http.get(self.companyfacts_url.format(cik=cik))
        safe_url = getattr(response, "safe_url", response.url)
        payload = response.json()
        raw_path = self._storage.write_raw_json(run_date, "sec_companyfacts", ticker.lower(), payload)
        result = (cik, payload, safe_url, raw_path, None)
        self._companyfacts_payload_cache[cache_key] = result
        return result

    def fetch_company_facts(self, ticker: str, run_date: date) -> SourcePayload[FundamentalSnapshot | None]:
        ingested_at = datetime.now(timezone.utc)
        cik, payload, safe_url, raw_path, error = self._get_companyfacts_payload(ticker, run_date)
        if error:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="sec_companyfacts",
                    success=False,
                    partial=True,
                    message=error,
                    ingested_at=ingested_at,
                ),
            )

        snapshot = _build_snapshot(ticker, cik, payload, safe_url, ingested_at)
        status = SourceStatus(
            source="sec_companyfacts",
            success=snapshot is not None,
            partial=snapshot is None,
            message="ok" if snapshot else "no usable companyfacts metrics",
            payload_path=str(raw_path) if raw_path else None,
            source_url=safe_url,
            ingested_at=ingested_at,
            event_start=datetime.combine(snapshot.period_end, datetime.min.time(), tzinfo=timezone.utc) if snapshot and snapshot.period_end else None,
            event_end=datetime.combine(snapshot.filed_on, datetime.min.time(), tzinfo=timezone.utc) if snapshot and snapshot.filed_on else None,
        )
        return SourcePayload(data=snapshot, status=status, raw_path=raw_path)

    def fetch_valuation_fundamentals(
        self, ticker: str, run_date: date
    ) -> SourcePayload[ValuationFundamentals | None]:
        """Richer SEC extraction for the valuation data layer — Layer 2 evidence only.

        Reuses the same companyfacts payload as fetch_company_facts (see
        _get_companyfacts_payload) rather than issuing a second HTTP request.
        """
        ingested_at = datetime.now(timezone.utc)
        cik, payload, safe_url, raw_path, error = self._get_companyfacts_payload(ticker, run_date)
        if error:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="sec_valuation_fundamentals",
                    success=False,
                    partial=True,
                    message=error,
                    ingested_at=ingested_at,
                ),
            )

        valuation = _build_valuation_fundamentals(ticker, cik, payload, safe_url, ingested_at)
        status = SourceStatus(
            source="sec_valuation_fundamentals",
            success=valuation is not None,
            partial=valuation is None,
            message="ok" if valuation else "no usable companyfacts concepts",
            payload_path=str(raw_path) if raw_path else None,
            source_url=safe_url,
            ingested_at=ingested_at,
        )
        return SourcePayload(data=valuation, status=status, raw_path=raw_path)


def _parse_acceptance_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _build_snapshot(
    ticker: str,
    cik: str,
    payload: dict,
    source_url: str,
    ingested_at: datetime,
) -> FundamentalSnapshot | None:
    us_gaap = payload.get("facts", {}).get("us-gaap", {})
    revenue_latest, revenue_previous, revenue_meta = _extract_latest_pair(
        us_gaap,
        ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    )
    ocf_latest, ocf_previous, ocf_meta = _extract_latest_pair(
        us_gaap,
        ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    )
    capex_latest, _, capex_meta = _extract_latest_pair(
        us_gaap,
        ["PaymentsToAcquirePropertyPlantAndEquipment", "PropertyPlantAndEquipmentAdditions"],
    )
    cash_latest, _, cash_meta = _extract_latest_pair(
        us_gaap,
        ["CashAndCashEquivalentsAtCarryingValue"],
    )
    debt_latest, _, debt_meta = _extract_latest_pair(
        us_gaap,
        ["LongTermDebtAndCapitalLeaseObligations", "LongTermDebtNoncurrent", "LongTermDebt"],
    )

    metric_values = [revenue_latest, ocf_latest, capex_latest, cash_latest, debt_latest]
    if not any(value is not None for value in metric_values):
        return None

    period_end = revenue_meta.get("end") or ocf_meta.get("end") or cash_meta.get("end")
    filed_on = revenue_meta.get("filed") or ocf_meta.get("filed") or cash_meta.get("filed")
    notes = [
        note
        for note in [
            revenue_meta.get("metric"),
            ocf_meta.get("metric"),
            capex_meta.get("metric"),
            cash_meta.get("metric"),
            debt_meta.get("metric"),
        ]
        if note
    ]
    return FundamentalSnapshot(
        ticker=ticker,
        cik=cik,
        period_end=_parse_date(period_end),
        filed_on=_parse_date(filed_on),
        revenue_latest=revenue_latest,
        revenue_previous=revenue_previous,
        operating_cashflow_latest=ocf_latest,
        operating_cashflow_previous=ocf_previous,
        capex_latest=abs(capex_latest) if capex_latest is not None else None,
        cash_latest=cash_latest,
        debt_latest=debt_latest,
        source="sec_companyfacts",
        source_url=source_url,
        ingested_at=ingested_at,
        notes=notes,
    )


def _extract_latest_pair(us_gaap: dict, metric_names: list[str]) -> tuple[float | None, float | None, dict]:
    for metric_name in metric_names:
        fact = us_gaap.get(metric_name)
        if not fact:
            continue
        units = fact.get("units", {})
        values = units.get("USD") or units.get("USD/shares") or []
        normalized = []
        for entry in values:
            form = entry.get("form")
            if form not in {"10-K", "10-Q", "20-F", "6-K"}:
                continue
            end = _parse_date(entry.get("end"))
            filed = _parse_date(entry.get("filed"))
            val = entry.get("val")
            if end is None or val is None:
                continue
            fy = entry.get("fy")
            fp = entry.get("fp")
            try:
                fiscal_year = int(fy) if fy is not None else None
            except (TypeError, ValueError):
                fiscal_year = None
            normalized.append(
                {
                    "end": end,
                    "filed": filed or end,
                    "value": float(val),
                    "fy": fiscal_year,
                    "fp": fp,
                    "form": form,
                }
            )
        if not normalized:
            continue
        normalized.sort(key=lambda item: (item["end"], item["filed"]), reverse=True)
        latest = normalized[0]
        previous = None
        for candidate in normalized[1:]:
            if (
                latest["fp"]
                and latest["fy"] is not None
                and candidate["fp"] == latest["fp"]
                and candidate["fy"] == latest["fy"] - 1
            ):
                previous = candidate
                break
        return (
            latest["value"],
            previous["value"] if previous else None,
            {
                "metric": metric_name,
                "end": latest["end"].isoformat(),
                "filed": latest["filed"].isoformat(),
                "fp": latest.get("fp"),
                "fy": latest.get("fy"),
                "comparable": previous is not None,
            },
        )
    return None, None, {}


def _tag_latest_end(namespace_facts: dict, tag: str) -> date | None:
    """Most recent ``end`` date reported anywhere under ``tag`` (any recognised unit
    key, any accepted form). Used only to judge tag recency for ``_select_tag_order``
    -- NOT a substitute for the real per-unit, per-period extraction each concept
    function still performs afterward.
    """
    fact = namespace_facts.get(tag)
    if not fact:
        return None
    latest: date | None = None
    units = fact.get("units", {})
    for unit_name in ("USD", "shares", "USD/shares", "pure"):
        for entry in units.get(unit_name) or []:
            if entry.get("form") not in _ACCEPTED_FORMS:
                continue
            end = _parse_date(entry.get("end"))
            if end is not None and (latest is None or end > latest):
                latest = end
    return latest


def _determine_anchor_period(us_gaap: dict) -> date | None:
    """Filer's most-recently-reported period end, used as the recency reference point
    for stale-tag detection (see ``_STALE_TAG_WINDOW_DAYS`` / ``_select_tag_order``).

    Computed from the union of revenue's candidate tags plus the cash tag -- both are
    near-universal, every-filing concepts, so the max across them is a robust "how
    current is this filer's most recent filing" signal even when one of the two is
    reported on an unusual cadence for a particular filer. Returns ``None`` (recency
    cannot be judged) when neither is present at all, e.g. a payload with no revenue
    or cash data -- callers must treat that as "cannot assess staleness", not "no
    candidates are fresh".
    """
    anchor_tags = [*_FLOW_CONCEPT_TAGS["revenue"], *_INSTANT_CONCEPT_TAGS["cash_and_equivalents"]]
    latest: date | None = None
    for tag in anchor_tags:
        end = _tag_latest_end(us_gaap, tag)
        if end is not None and (latest is None or end > latest):
            latest = end
    return latest


def _select_tag_order(
    namespace_facts: dict, tags: list[str], anchor: date | None
) -> tuple[list[str], set[str]]:
    """Reorder a concept's candidate tags so any tag reporting within
    ``_STALE_TAG_WINDOW_DAYS`` of the filer's anchor period (a "fresh" tag) is tried
    before a tag whose own latest datapoint falls outside that window (a "stale" tag)
    -- WITHOUT otherwise disturbing the documented priority order within either group.
    This is deliberately the only thing recency changes: two fresh tags are never
    reordered relative to each other, and a stale tag is never dropped from
    consideration outright -- it can still win if nothing fresher has any data at all
    (see ``_extract_concept_history`` / ``_extract_flow_concept_history`` for that
    fallback contract).

    Returns ``(ordered_tags, stale_tag_names)``. When ``anchor`` is ``None`` (the
    filer's own revenue/cash tags gave no usable reference date), recency cannot be
    judged at all, so ``tags`` is returned unchanged and nothing is treated as stale --
    this preserves the historical "first tag with data wins" behaviour exactly for
    that case.
    """
    if anchor is None:
        return tags, set()
    fresh = [
        tag
        for tag in tags
        if (latest := _tag_latest_end(namespace_facts, tag)) is not None
        and (anchor - latest).days <= _STALE_TAG_WINDOW_DAYS
    ]
    stale = [tag for tag in tags if tag not in fresh]
    return fresh + stale, set(stale)


def _annotate_stale_tag(tag: str, namespace_facts: dict, anchor: date | None) -> str:
    """Bake staleness visibly into the winning tag name when ``_select_tag_order`` had
    to fall back to a tag outside the freshness window (no fresher candidate had any
    data) -- so a stale read is always visible on ``ConceptHistory.tag``, never
    silently indistinguishable from an ordinary fresh match."""
    if anchor is None:
        return tag
    latest = _tag_latest_end(namespace_facts, tag)
    if latest is None:
        detail = "tag has no dated datapoints to compare against the anchor period"
    else:
        gap_days = (anchor - latest).days
        detail = f"latest datapoint {latest.isoformat()}, {gap_days}d before anchor {anchor.isoformat()}"
    return (
        f"{tag} [{_STALE_FALLBACK_MARKER}: {detail}; no candidate tag reported within "
        f"{_STALE_TAG_WINDOW_DAYS}d of the anchor period]"
    )


def _build_valuation_fundamentals(
    ticker: str,
    cik: str | None,
    payload: dict,
    source_url: str | None,
    ingested_at: datetime,
) -> ValuationFundamentals | None:
    us_gaap = payload.get("facts", {}).get("us-gaap", {})
    dei = payload.get("facts", {}).get("dei", {})

    # Reference point for recency-aware tag selection across every multi-tag concept
    # below (see _STALE_TAG_WINDOW_DAYS / _select_tag_order). Computed once per filer
    # rather than per-concept since it is the same anchor for all of them.
    anchor = _determine_anchor_period(us_gaap)

    concepts: dict[str, ConceptHistory] = {}
    for concept, tags in _FLOW_CONCEPT_TAGS.items():
        if concept in _ADDITIVE_FLOW_CONCEPTS:
            history = _extract_flow_concept_history(us_gaap, concept, tags, anchor=anchor)
        else:
            history = _extract_concept_history(us_gaap, concept, tags, require_duration=True, anchor=anchor)
        if history is not None:
            concepts[concept] = history

    # sga_expense's ordered-candidate-tag pass above only tries the combined tag (see
    # _FLOW_CONCEPT_TAGS' comment). If that produced nothing, try the split-tag-sum
    # fallback -- deliberately a separate path, not another candidate in the ordered
    # list, because it can only be used when BOTH split tags cover a period (see
    # _extract_sga_split_tag_history's docstring). Recency-awareness deliberately does
    # NOT apply to this two-tag-sum path -- it is already period-gated (only periods
    # both tags cover are used), which is a stronger freshness guarantee than the
    # candidate-list staleness window.
    if "sga_expense" not in concepts:
        sga_history = _extract_sga_split_tag_history(us_gaap)
        if sga_history is not None:
            concepts["sga_expense"] = sga_history

    for concept, tags in _INSTANT_CONCEPT_TAGS.items():
        history = _extract_concept_history(us_gaap, concept, tags, require_duration=False, anchor=anchor)
        if history is not None:
            concepts[concept] = history

    cover_total, cover_classes, cover_meta = _extract_cover_page_shares(dei)

    if not concepts and cover_total is None:
        return None

    notes: list[str] = []
    data_gaps: list[str] = []
    gross_ppe_history = concepts.get("gross_ppe")
    if gross_ppe_history is not None and gross_ppe_history.tag.startswith("PropertyPlantAndEquipmentNet"):
        notes.append(
            "gross_ppe fell back to PropertyPlantAndEquipmentNet (net, not gross) because "
            "PropertyPlantAndEquipmentGross was not reported by this filer. Beneish's AQI and "
            "DEPI ratios are defined in terms of gross PP&E -- treat those two ratios with extra "
            "caution for this ticker."
        )

    for concept, history in concepts.items():
        if _STALE_FALLBACK_MARKER not in history.tag:
            continue
        notes.append(
            f"{concept} found no candidate us-gaap tag with data reported within "
            f"{_STALE_TAG_WINDOW_DAYS}d of this filer's anchor period "
            f"({anchor.isoformat() if anchor else '?'}) and fell back to a stale tag -- "
            f"see ConceptHistory.tag for which one and how stale. Treat this figure with "
            f"caution; it may understate the true current balance."
        )
        data_gaps.append(f"{concept}: stale tag fallback ({history.tag})")

    return ValuationFundamentals(
        ticker=ticker,
        cik=cik,
        source="sec_companyfacts",
        source_url=source_url,
        ingested_at=ingested_at,
        concepts=concepts,
        cover_page_shares=cover_total,
        cover_page_share_classes=cover_classes,
        cover_page_meta=cover_meta,
        notes=notes,
        data_gaps=data_gaps,
    )


def _extract_concept_history(
    namespace_facts: dict,
    concept: str,
    tags: list[str],
    *,
    require_duration: bool,
    max_items: int = _MAX_HISTORY_ITEMS,
    anchor: date | None = None,
) -> ConceptHistory | None:
    """Multi-tag fallback extraction of up to ``max_items`` deduped datapoints.

    Tries each candidate tag in priority order, restricted first to tags that are
    "fresh" relative to ``anchor`` (see ``_select_tag_order``) -- the first one with
    usable data wins. If no fresh candidate yields data, every candidate (including
    stale ones) is tried in the original order as a fallback, and the winning tag is
    annotated via ``_annotate_stale_tag`` so a stale read is recorded on
    ``ConceptHistory.tag`` rather than silently indistinguishable from a fresh match.
    Duplicate and amended datapoints for the same ``end`` are deduped deterministically
    by keeping the entry with the newest ``filed`` date. For flow (duration) concepts,
    ``require_duration=True`` keeps only entries shaped like a single fiscal quarter
    (60-100 day span) so TTM sums are never built from an annual 10-K tag by mistake.

    This function is used for instant (point-in-time) concepts and for the two
    non-additive flow concepts (diluted/basic weighted-average share counts), which are
    period *averages* and cannot be reconstructed from cumulative facts by subtraction.
    All other flow concepts (revenue, cash flow, capex, SBC, D&A, ...) go through
    ``_extract_flow_concept_history`` instead, which derives missing standalone quarters
    (e.g. an unreported Q4, or a Q2/Q3 tagged year-to-date) from ``start``/``end`` date
    arithmetic on the cumulative facts filers do report — see that function's docstring.
    """
    ordered_tags, stale_tags = _select_tag_order(namespace_facts, tags, anchor)
    for tag in ordered_tags:
        fact = namespace_facts.get(tag)
        if not fact:
            continue
        units = fact.get("units", {})
        entries = None
        matched_unit = None
        for unit_name in ("USD", "shares", "USD/shares", "pure"):
            candidate_entries = units.get(unit_name)
            if candidate_entries:
                entries = candidate_entries
                matched_unit = unit_name
                break
        if not entries:
            continue

        by_end: dict[date, dict] = {}
        for entry in entries:
            form = entry.get("form")
            if form not in _ACCEPTED_FORMS:
                continue
            end = _parse_date(entry.get("end"))
            val = entry.get("val")
            if end is None or val is None:
                continue
            start = _parse_date(entry.get("start"))
            if require_duration:
                if start is None:
                    continue
                duration_days = (end - start).days
                if not (_QUARTER_MIN_DAYS <= duration_days <= _QUARTER_MAX_DAYS):
                    continue
            filed = _parse_date(entry.get("filed")) or end
            candidate = {
                "end": end,
                "filed": filed,
                "value": float(val),
                "form": form,
                "fy": _safe_int(entry.get("fy")),
                "fp": entry.get("fp"),
                "frame": entry.get("frame"),
            }
            existing = by_end.get(end)
            if existing is None or filed > existing["filed"]:
                by_end[end] = candidate

        if not by_end:
            continue

        ordered = sorted(by_end.values(), key=lambda item: item["end"], reverse=True)[:max_items]
        datapoints = [
            ConceptDatapoint(
                end=item["end"],
                filed=item["filed"],
                value=item["value"],
                form=item["form"],
                fy=item["fy"],
                fp=item["fp"],
                frame=item["frame"],
            )
            for item in ordered
        ]
        resolved_tag = _annotate_stale_tag(tag, namespace_facts, anchor) if tag in stale_tags else tag
        return ConceptHistory(concept=concept, tag=resolved_tag, unit=matched_unit or "", datapoints=datapoints)
    return None


# Duration buckets in (end - start) days, used by the quarterization normaliser below.
# A filer's own quarterly facts land in "quarter"; year-to-date cumulative facts land in
# "half" / "three_quarters" / "year" and are used only to derive a missing standalone
# quarter, never returned as a datapoint themselves.
_DURATION_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("quarter", 85, 95),
    ("half", 175, 190),
    ("three_quarters", 265, 280),
    ("year", 355, 375),
)


def _bucket_duration_days(days: int) -> str | None:
    for name, low, high in _DURATION_BUCKETS:
        if low <= days <= high:
            return name
    return None


def _quarterize_duration_entries(entries: list[dict]) -> dict[date, dict]:
    """Turn a filer's raw duration facts for one tag into a standalone-quarter series.

    Real SEC XBRL data has two recurring shapes that silently break a naive
    "keep only ~90-day entries" filter:

    1. 10-K filers often never report a standalone Q4 duration fact — Q4 only exists
       inside the ~365-day FY total, leaving a hole in the quarterly series every year.
    2. Many filers tag Q2/Q3 cash-flow-statement amounts (operating cashflow, capex,
       SBC, D&A) as year-to-date cumulative ("six months ended", "nine months ended")
       rather than discrete, so only the Q1-shaped entry survives a duration-only filter.

    Both are the same underlying problem: a missing quarter whose value is recoverable
    by subtracting two cumulative facts that share the same period ``start`` (fiscal
    year start). This function does that subtraction using plain date arithmetic on
    ``start``/``end`` — not the ``fy``/``fp`` XBRL labels, which are unreliable for
    pairing (comparative-period entries are often tagged with the filing's fiscal year,
    not their own).

    Entries are first deduped by the full ``(start, end)`` period (not ``end`` alone —
    a half-year fact and a standalone quarter fact can share the same ``end`` with
    different ``start``, and collapsing them by ``end`` would silently drop one), keeping
    the newest ``filed`` per period to handle amendments. The final result, however, is
    keyed by ``end`` only (one datapoint per period-ending date): a reported quarter-
    shaped (85-95 day) period always wins over a derived value ending on the same date
    even if their ``start`` dates differ by a day or two (e.g. a standalone Q3 starting
    2025-08-01 vs. a Q3 implied by subtracting H1 ending 2025-07-31 from 9M — both
    describe "the third quarter" and must collapse to one datapoint, not two).

    Returns a dict keyed by ``end`` of quarter-period dicts, each carrying ``derived``
    (bool) and, when derived, ``derived_from`` (a human-readable trace of which two
    cumulative facts were subtracted).
    """
    by_period: dict[tuple[date, date], dict] = {}
    for entry in entries:
        key = (entry["start"], entry["end"])
        existing = by_period.get(key)
        if existing is None or entry["filed"] > existing["filed"]:
            by_period[key] = entry

    quarters: dict[date, dict] = {}
    by_bucket: dict[str, list[dict]] = {"quarter": [], "half": [], "three_quarters": [], "year": []}
    for item in by_period.values():
        duration_days = (item["end"] - item["start"]).days
        bucket = _bucket_duration_days(duration_days)
        if bucket is None:
            continue
        by_bucket[bucket].append(item)
        if bucket == "quarter":
            quarters[item["end"]] = {**item, "derived": False, "derived_from": None}

    def _index_by_start(items: list[dict]) -> dict[date, list[dict]]:
        index: dict[date, list[dict]] = {}
        for item in items:
            index.setdefault(item["start"], []).append(item)
        return index

    quarter_by_start = _index_by_start(by_bucket["quarter"])
    half_by_start = _index_by_start(by_bucket["half"])
    three_q_by_start = _index_by_start(by_bucket["three_quarters"])
    year_by_start = _index_by_start(by_bucket["year"])

    def _derive(shorter_by_start: dict[date, list[dict]], longer_items: list[dict], label_short: str, label_long: str) -> None:
        """Derive ``longer - shorter`` for every (shorter, longer) pair sharing a start."""
        longer_by_start = _index_by_start(longer_items)
        for start, shorters in shorter_by_start.items():
            longers = longer_by_start.get(start)
            if not longers:
                continue
            # The cumulative fact that begins exactly where the shorter one begins and
            # ends soonest is the correct pairing (guards against duplicate/odd entries).
            shorter = min(shorters, key=lambda item: item["end"])
            for longer in longers:
                if longer["end"] <= shorter["end"]:
                    continue
                if longer["end"] in quarters:
                    continue  # a reported (or already-derived) quarter already covers this end date
                quarters[longer["end"]] = {
                    "start": shorter["end"],
                    "end": longer["end"],
                    "value": longer["value"] - shorter["value"],
                    "filed": longer["filed"],
                    "form": longer["form"],
                    "fy": longer.get("fy"),
                    "fp": None,
                    "frame": None,
                    "derived": True,
                    "derived_from": (
                        f"{label_long}({longer['start'].isoformat()}->{longer['end'].isoformat()}) - "
                        f"{label_short}({shorter['start'].isoformat()}->{shorter['end'].isoformat()})"
                    ),
                }

    # Q2 = H1 - Q1 (same start)
    _derive(quarter_by_start, by_bucket["half"], "quarter", "half")
    # Q3 = 9M - H1 (same start)
    _derive(half_by_start, by_bucket["three_quarters"], "half", "9M")
    # Q4 = FY - 9M (same start)
    _derive(three_q_by_start, by_bucket["year"], "9M", "FY")

    return quarters


def _extract_flow_concept_history(
    namespace_facts: dict,
    concept: str,
    tags: list[str],
    max_items: int = _MAX_HISTORY_ITEMS,
    *,
    anchor: date | None = None,
) -> ConceptHistory | None:
    """Extraction for period-additive flow concepts, routed through the quarterization
    normaliser (``_quarterize_duration_entries``) so a missing standalone quarter is
    derived from cumulative facts instead of leaving a gap. See that function's docstring
    for the two real SEC XBRL patterns this fixes.

    Tries each candidate tag in priority order, restricted first to tags "fresh"
    relative to ``anchor`` (see ``_select_tag_order``); the first tag that yields at
    least one usable quarter (reported or derived) wins. If no fresh candidate yields a
    usable quarter, every candidate (including stale ones) is tried in the original
    order as a fallback, and the winning tag is annotated via ``_annotate_stale_tag``
    when it came from the stale group.
    """
    ordered_tags, stale_tags = _select_tag_order(namespace_facts, tags, anchor)
    for tag in ordered_tags:
        fact = namespace_facts.get(tag)
        if not fact:
            continue
        parsed, matched_unit = _parse_flow_fact_entries(fact)
        if not parsed:
            continue

        quarters = _quarterize_duration_entries(parsed)
        if not quarters:
            continue

        ordered = sorted(quarters.values(), key=lambda item: item["end"], reverse=True)[:max_items]
        datapoints = [
            ConceptDatapoint(
                end=item["end"],
                filed=item["filed"],
                value=item["value"],
                form=item["form"],
                fy=item.get("fy"),
                fp=item.get("fp"),
                frame=item.get("frame"),
                start=item.get("start"),
                derived=item.get("derived", False),
                derived_from=item.get("derived_from"),
            )
            for item in ordered
        ]
        resolved_tag = _annotate_stale_tag(tag, namespace_facts, anchor) if tag in stale_tags else tag
        return ConceptHistory(concept=concept, tag=resolved_tag, unit=matched_unit or "", datapoints=datapoints)
    return None


def _parse_flow_fact_entries(fact: dict) -> tuple[list[dict], str | None]:
    """Parse one us-gaap fact's raw duration entries into the flat dict shape the flow-
    concept pipeline (``_quarterize_duration_entries`` + ``ConceptDatapoint`` construction)
    expects. Shared by ``_extract_flow_concept_history`` (single ordered-candidate-tag
    path) and ``_extract_sga_split_tag_history`` (two-tag-sum fallback path) so both go
    through identical parsing/filtering rules -- accepted-forms check, start/end/val
    presence, ``filed`` defaulting to ``end`` when absent.

    Returns (parsed_entries, matched_unit_name). ``matched_unit_name`` is ``None`` when
    the fact has no entries under any of the recognised unit keys.
    """
    units = fact.get("units", {})
    entries = None
    matched_unit = None
    for unit_name in ("USD", "shares", "USD/shares", "pure"):
        candidate_entries = units.get(unit_name)
        if candidate_entries:
            entries = candidate_entries
            matched_unit = unit_name
            break
    if not entries:
        return [], None

    parsed: list[dict] = []
    for entry in entries:
        form = entry.get("form")
        if form not in _ACCEPTED_FORMS:
            continue
        start = _parse_date(entry.get("start"))
        end = _parse_date(entry.get("end"))
        val = entry.get("val")
        if start is None or end is None or val is None:
            continue
        filed = _parse_date(entry.get("filed")) or end
        parsed.append(
            {
                "start": start,
                "end": end,
                "value": float(val),
                "filed": filed,
                "form": form,
                "fy": _safe_int(entry.get("fy")),
                "fp": entry.get("fp"),
                "frame": entry.get("frame"),
            }
        )
    return parsed, matched_unit


def _dedupe_entries_by_period(entries: list[dict]) -> dict[tuple[date, date], dict]:
    """Dedupe a flat list of parsed duration entries by their full ``(start, end)``
    period, keeping the entry with the newest ``filed`` per period -- the same
    amended-datapoint handling ``_quarterize_duration_entries`` applies internally for a
    single tag's entries, reused here so the two-tag SG&A sum starts from one clean,
    deduped value per (tag, period) before summing."""
    by_period: dict[tuple[date, date], dict] = {}
    for entry in entries:
        key = (entry["start"], entry["end"])
        existing = by_period.get(key)
        if existing is None or entry["filed"] > existing["filed"]:
            by_period[key] = entry
    return by_period


def _extract_sga_split_tag_history(
    namespace_facts: dict, max_items: int = _MAX_HISTORY_ITEMS
) -> ConceptHistory | None:
    """Fallback for filers that never report a combined SellingGeneralAndAdministrative-
    Expense figure and instead split selling costs from G&A into two separate tags
    (``GeneralAndAdministrativeExpense`` + ``SellingAndMarketingExpense``, see
    ``_SGA_SPLIT_TAGS``).

    Only called by ``_build_valuation_fundamentals`` after the ordinary combined-tag
    extraction (via ``_FLOW_CONCEPT_TAGS['sga_expense']``) has already come back with
    nothing -- this is a deliberately separate path, not another entry appended to that
    ordered candidate list, because summing is only valid, and only attempted, when BOTH
    split tags report data for the SAME ``(start, end)`` period. A period covered by just
    one of the two tags is dropped rather than used alone: treating "G&A only" or
    "Selling only" as if it were the filer's whole SG&A would silently understate the
    numerator for that quarter and corrupt the year-over-year SGAI ratio Beneish depends
    on (mixing a summed value for some periods with a single-tag value for others in the
    same history is exactly the corruption this avoids).

    The returned ``ConceptHistory.tag`` records the sum explicitly (rather than reusing
    either underlying tag name) so this choice is visible to anything inspecting the
    data, never silently indistinguishable from a normal single-tag match.
    """
    ga_fact = namespace_facts.get(_SGA_SPLIT_TAGS[0])
    selling_fact = namespace_facts.get(_SGA_SPLIT_TAGS[1])
    if not ga_fact or not selling_fact:
        return None

    ga_entries, ga_unit = _parse_flow_fact_entries(ga_fact)
    selling_entries, selling_unit = _parse_flow_fact_entries(selling_fact)
    if not ga_entries or not selling_entries:
        return None

    ga_by_period = _dedupe_entries_by_period(ga_entries)
    selling_by_period = _dedupe_entries_by_period(selling_entries)
    common_periods = set(ga_by_period) & set(selling_by_period)
    if not common_periods:
        return None

    combined_entries: list[dict] = []
    for key in common_periods:
        ga_entry = ga_by_period[key]
        selling_entry = selling_by_period[key]
        combined_entries.append(
            {
                "start": key[0],
                "end": key[1],
                "value": ga_entry["value"] + selling_entry["value"],
                "filed": max(ga_entry["filed"], selling_entry["filed"]),
                "form": ga_entry["form"],
                "fy": ga_entry.get("fy"),
                "fp": None,
                "frame": None,
            }
        )

    quarters = _quarterize_duration_entries(combined_entries)
    if not quarters:
        return None

    ordered = sorted(quarters.values(), key=lambda item: item["end"], reverse=True)[:max_items]
    datapoints = [
        ConceptDatapoint(
            end=item["end"],
            filed=item["filed"],
            value=item["value"],
            form=item["form"],
            fy=item.get("fy"),
            fp=item.get("fp"),
            frame=item.get("frame"),
            start=item.get("start"),
            derived=item.get("derived", False),
            derived_from=item.get("derived_from"),
        )
        for item in ordered
    ]
    return ConceptHistory(
        concept="sga_expense",
        tag=f"{_SGA_SPLIT_TAGS[0]}+{_SGA_SPLIT_TAGS[1]} (summed; combined SellingGeneralAndAdministrativeExpense "
        "tag not reported by this filer)",
        unit=ga_unit or selling_unit or "",
        datapoints=datapoints,
    )


def _extract_cover_page_shares(dei_facts: dict) -> tuple[float | None, list[ShareClassEntry], dict]:
    """Sum dei:EntityCommonStockSharesOutstanding across share classes for the latest filing.

    Companyfacts does not label which class/member each entry belongs to, but distinct
    entries filed in the same accession (``accn``) with distinct values are, in practice,
    the per-class cover-page counts for a dual/multi-class filer. Summing them recovers the
    true total share count; reading only the first entry (the naive single-class read)
    silently drops the other classes.
    """
    fact = dei_facts.get(_DEI_COVER_SHARES_TAG)
    if not fact:
        return None, [], {}
    units = fact.get("units", {})
    entries = units.get("shares") or []

    valid = []
    for entry in entries:
        form = entry.get("form")
        if form not in _ACCEPTED_FORMS:
            continue
        end = _parse_date(entry.get("end"))
        val = entry.get("val")
        accn = entry.get("accn")
        if end is None or val is None or not accn:
            continue
        filed = _parse_date(entry.get("filed")) or end
        valid.append({"end": end, "value": float(val), "accn": accn, "filed": filed})

    if not valid:
        return None, [], {}

    latest_filed = max(item["filed"] for item in valid)
    latest_accn = next(item["accn"] for item in valid if item["filed"] == latest_filed)
    group = [item for item in valid if item["accn"] == latest_accn]

    unique: dict[tuple[date, float], dict] = {}
    for item in group:
        unique[(item["end"], item["value"])] = item
    classes = sorted(unique.values(), key=lambda item: item["value"], reverse=True)

    total = sum(item["value"] for item in classes)
    breakdown = [ShareClassEntry(value=item["value"], as_of=item["end"]) for item in classes]
    meta = {
        "tag": f"dei:{_DEI_COVER_SHARES_TAG}",
        "accn": latest_accn,
        "class_count": len(classes),
        "as_of": classes[0]["end"].isoformat() if classes else None,
    }
    return total, breakdown, meta


def _safe_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
