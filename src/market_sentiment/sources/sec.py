from __future__ import annotations

from datetime import date, datetime, timezone

from market_sentiment.http import HttpClient
from market_sentiment.models import FundamentalSnapshot, OfficialEvent, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.storage import Storage


class SecClient:
    ticker_map_url = "https://www.sec.gov/files/company_tickers_exchange.json"
    submissions_url = "https://data.sec.gov/submissions/CIK{cik}.json"
    companyfacts_url = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    filing_index = "https://www.sec.gov/Archives/edgar/data/{cik_numeric}/{accession}/{primary_document}"

    def __init__(self, http_client: HttpClient, storage: Storage) -> None:
        self._http = http_client
        self._storage = storage
        self._ticker_map_cache: dict[str, str] | None = None

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
        items = zip(
            forms,
            filing_dates,
            accession_numbers,
            acceptance_datetimes,
            primary_documents,
            descriptions,
            strict=False,
        )
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
        status = SourceStatus(
            source="sec",
            success=bool(events),
            partial=not bool(events),
            message="ok" if events else "empty recent filings",
            payload_path=str(raw_path),
            source_url=safe_url,
            ingested_at=ingested_at,
            event_start=min((event.event_time for event in events), default=None),
            event_end=max((event.event_time for event in events), default=None),
        )
        return SourcePayload(data=events, status=status, raw_path=raw_path)

    def fetch_company_facts(self, ticker: str, run_date: date) -> SourcePayload[FundamentalSnapshot | None]:
        ingested_at = datetime.now(timezone.utc)
        try:
            cik = self.cik_for_ticker(ticker, run_date)
        except RuntimeError as exc:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="sec_companyfacts",
                    success=False,
                    partial=True,
                    message=f"Failed to load SEC ticker map for {ticker}: {exc}",
                    ingested_at=ingested_at,
                ),
            )
        if not cik:
            return SourcePayload(
                data=None,
                status=SourceStatus(
                    source="sec_companyfacts",
                    success=False,
                    partial=True,
                    message=f"No CIK mapping for {ticker}",
                    ingested_at=ingested_at,
                ),
            )

        response = self._http.get(self.companyfacts_url.format(cik=cik))
        safe_url = getattr(response, "safe_url", response.url)
        payload = response.json()
        raw_path = self._storage.write_raw_json(run_date, "sec_companyfacts", ticker.lower(), payload)
        snapshot = _build_snapshot(ticker, cik, payload, safe_url, ingested_at)
        status = SourceStatus(
            source="sec_companyfacts",
            success=snapshot is not None,
            partial=snapshot is None,
            message="ok" if snapshot else "no usable companyfacts metrics",
            payload_path=str(raw_path),
            source_url=safe_url,
            ingested_at=ingested_at,
            event_start=datetime.combine(snapshot.period_end, datetime.min.time(), tzinfo=timezone.utc) if snapshot and snapshot.period_end else None,
            event_end=datetime.combine(snapshot.filed_on, datetime.min.time(), tzinfo=timezone.utc) if snapshot and snapshot.filed_on else None,
        )
        return SourcePayload(data=snapshot, status=status, raw_path=raw_path)


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


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
