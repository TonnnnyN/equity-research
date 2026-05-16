"""Point-in-time FundamentalSnapshot extraction from SEC companyfacts payloads.

The production `sec.py` builds a snapshot from the *latest* available filing.
For a backtest we must only use data that was already filed as of the decision
date, otherwise the fundamentals/risk buckets leak future information.

`extract_snapshot_asof` mirrors `sec._build_snapshot` but drops every XBRL
entry whose `filed` date is after the cutoff. `build_fundamentals_timeline`
precomputes one snapshot per distinct filing date so the status engine can do a
cheap as-of lookup instead of re-parsing a multi-MB payload per day.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from market_sentiment.models import FundamentalSnapshot

_RELEVANT_FORMS = {"10-K", "10-Q", "20-F", "6-K"}

_REVENUE_METRICS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
_OCF_METRICS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]
_CAPEX_METRICS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PropertyPlantAndEquipmentAdditions",
]
_CASH_METRICS = ["CashAndCashEquivalentsAtCarryingValue"]
_DEBT_METRICS = [
    "LongTermDebtAndCapitalLeaseObligations",
    "LongTermDebtNoncurrent",
    "LongTermDebt",
]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _extract_pair_asof(
    us_gaap: dict,
    metric_names: list[str],
    cutoff: date,
) -> tuple[float | None, float | None, dict]:
    """Latest + prior-year value for a metric, ignoring entries filed after cutoff."""
    for metric_name in metric_names:
        fact = us_gaap.get(metric_name)
        if not fact:
            continue
        units = fact.get("units", {})
        values = units.get("USD") or units.get("USD/shares") or []
        normalized = []
        for entry in values:
            if entry.get("form") not in _RELEVANT_FORMS:
                continue
            end = _parse_date(entry.get("end"))
            filed = _parse_date(entry.get("filed"))
            val = entry.get("val")
            if end is None or val is None:
                continue
            effective_filed = filed or end
            if effective_filed > cutoff:
                continue
            fy = entry.get("fy")
            try:
                fiscal_year = int(fy) if fy is not None else None
            except (TypeError, ValueError):
                fiscal_year = None
            normalized.append(
                {
                    "end": end,
                    "filed": effective_filed,
                    "value": float(val),
                    "fy": fiscal_year,
                    "fp": entry.get("fp"),
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
                "end": latest["end"],
                "filed": latest["filed"],
            },
        )
    return None, None, {}


def extract_snapshot_asof(
    payload: dict,
    ticker: str,
    cik: str | None,
    cutoff: date,
) -> FundamentalSnapshot | None:
    """Build a FundamentalSnapshot using only filings filed on or before `cutoff`."""
    us_gaap = payload.get("facts", {}).get("us-gaap", {})
    if not us_gaap:
        return None

    revenue_latest, revenue_previous, revenue_meta = _extract_pair_asof(us_gaap, _REVENUE_METRICS, cutoff)
    ocf_latest, ocf_previous, ocf_meta = _extract_pair_asof(us_gaap, _OCF_METRICS, cutoff)
    capex_latest, _, capex_meta = _extract_pair_asof(us_gaap, _CAPEX_METRICS, cutoff)
    cash_latest, _, cash_meta = _extract_pair_asof(us_gaap, _CASH_METRICS, cutoff)
    debt_latest, _, _ = _extract_pair_asof(us_gaap, _DEBT_METRICS, cutoff)

    if not any(v is not None for v in (revenue_latest, ocf_latest, capex_latest, cash_latest, debt_latest)):
        return None

    period_end = revenue_meta.get("end") or ocf_meta.get("end") or cash_meta.get("end")
    filed_on = revenue_meta.get("filed") or ocf_meta.get("filed") or cash_meta.get("filed")
    notes = [
        m.get("metric")
        for m in (revenue_meta, ocf_meta, capex_meta, cash_meta)
        if m.get("metric")
    ]
    return FundamentalSnapshot(
        ticker=ticker,
        cik=cik,
        period_end=period_end,
        filed_on=filed_on,
        revenue_latest=revenue_latest,
        revenue_previous=revenue_previous,
        operating_cashflow_latest=ocf_latest,
        operating_cashflow_previous=ocf_previous,
        capex_latest=abs(capex_latest) if capex_latest is not None else None,
        cash_latest=cash_latest,
        debt_latest=debt_latest,
        source="sec_companyfacts_asof",
        source_url=None,
        ingested_at=datetime.now(timezone.utc),
        notes=notes,
    )


def _all_filed_dates(payload: dict) -> list[date]:
    us_gaap = payload.get("facts", {}).get("us-gaap", {})
    filed_dates: set[date] = set()
    for metric in (*_REVENUE_METRICS, *_OCF_METRICS, *_CAPEX_METRICS, *_CASH_METRICS, *_DEBT_METRICS):
        fact = us_gaap.get(metric)
        if not fact:
            continue
        for entry in fact.get("units", {}).get("USD", []):
            if entry.get("form") not in _RELEVANT_FORMS:
                continue
            filed = _parse_date(entry.get("filed")) or _parse_date(entry.get("end"))
            if filed is not None:
                filed_dates.add(filed)
    return sorted(filed_dates)


def build_fundamentals_timeline(
    payload: dict,
    ticker: str,
    cik: str | None,
) -> list[tuple[date, FundamentalSnapshot]]:
    """Return (effective_from, snapshot) pairs, one per distinct filing date.

    The status engine picks the snapshot with the greatest effective_from that
    is <= the decision date. Consecutive identical snapshots are collapsed.
    """
    timeline: list[tuple[date, FundamentalSnapshot]] = []
    for filed in _all_filed_dates(payload):
        snapshot = extract_snapshot_asof(payload, ticker, cik, filed)
        if snapshot is None:
            continue
        if timeline:
            prev = timeline[-1][1]
            if (
                prev.revenue_latest == snapshot.revenue_latest
                and prev.operating_cashflow_latest == snapshot.operating_cashflow_latest
                and prev.cash_latest == snapshot.cash_latest
                and prev.debt_latest == snapshot.debt_latest
            ):
                continue
        timeline.append((filed, snapshot))
    return timeline


def snapshot_asof(
    timeline: list[tuple[date, FundamentalSnapshot]],
    as_of: date,
) -> FundamentalSnapshot | None:
    """Pick the most recent snapshot whose effective_from <= as_of."""
    result: FundamentalSnapshot | None = None
    for effective_from, snapshot in timeline:
        if effective_from <= as_of:
            result = snapshot
        else:
            break
    return result
