"""Tests for the SEC companyfacts valuation-layer extraction in sources/sec.py.

All tests use fake HTTP payloads — no real network calls are made.
Test style follows test_sources.py / test_analyst_targets.py conventions.
"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from unittest import TestCase

from market_sentiment.sources.sec import SecClient
from market_sentiment.storage import Storage


class FakeResponse:
    def __init__(self, url: str, payload):
        self.url = url
        self.safe_url = url
        self._payload = payload

    def json(self):
        return self._payload


class RoutingFakeHttpClient:
    """Routes GET calls to a payload by URL prefix and counts calls per prefix."""

    def __init__(self, payloads: dict[str, dict]):
        self.payloads = payloads
        self.call_counts: dict[str, int] = {}

    def get(self, url: str, params=None, headers=None):
        for prefix, payload in self.payloads.items():
            if url.startswith(prefix):
                self.call_counts[prefix] = self.call_counts.get(prefix, 0) + 1
                return FakeResponse(url, payload)
        raise AssertionError(f"Unexpected URL: {url}")


def _make_storage(tmp: str) -> Storage:
    storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
    storage.init_db()
    return storage


def _ticker_map_payload() -> dict:
    return {"data": [[1585521, "ZOOM VIDEO COMMUNICATIONS INC", "ZM", "Nasdaq"]]}


def _quarterly_entries(tag_form: str, base_val: float, count: int = 5) -> list[dict]:
    """Build `count` quarterly (duration ~91 day) entries, most recent first."""
    ends = [
        (date(2026, 1, 31), date(2025, 11, 1)),
        (date(2025, 10, 31), date(2025, 8, 1)),
        (date(2025, 7, 31), date(2025, 5, 1)),
        (date(2025, 4, 30), date(2025, 2, 1)),
        (date(2025, 1, 31), date(2024, 11, 1)),
    ][:count]
    entries = []
    for index, (end, start) in enumerate(ends):
        entries.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "val": base_val + index * 5,
                "accn": f"0001585521-26-{100 + index:06d}",
                "fy": 2026 - index // 4,
                "fp": "Q1",
                "form": tag_form,
                "filed": (end.replace(day=28)).isoformat(),
                "frame": f"CY{end.year}Q{((end.month - 1) // 3) + 1}",
            }
        )
    return entries


def _instant_entry(end: date, val: float, accn: str = "0001585521-26-000100", form: str = "10-Q") -> dict:
    return {
        "end": end.isoformat(),
        "val": val,
        "accn": accn,
        "fy": 2026,
        "fp": "Q1",
        "form": form,
        "filed": end.replace(day=28).isoformat(),
    }


class MultiTagFallbackTests(TestCase):
    def test_falls_back_to_second_tag_when_first_missing(self) -> None:
        """revenue concept tries RevenueFromContractWithCustomerExcludingAssessedTax first;
        when only Revenues is present, that tag should win and be recorded on the history."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "Revenues": {"units": {"USD": _quarterly_entries("10-Q", 100.0)}},
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            self.assertTrue(result.status.success)
            self.assertIsNotNone(result.data)
            history = result.data.concepts["revenue"]
            self.assertEqual(history.tag, "Revenues")
            self.assertEqual(len(history.datapoints), 5)

    def test_third_candidate_tag_used_when_first_two_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "SalesRevenueNet": {"units": {"USD": _quarterly_entries("10-Q", 50.0)}},
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            self.assertIsNotNone(result.data)
            self.assertEqual(result.data.concepts["revenue"].tag, "SalesRevenueNet")


class DedupeAmendedDatapointsTests(TestCase):
    def test_keeps_newest_filed_entry_per_end_date(self) -> None:
        """Two entries share the same `end` (original 10-Q + a later 10-Q/A restatement);
        the restated value with the newer `filed` date must win, not be summed or averaged."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2025-11-01", "end": "2026-01-31", "val": 100.0,
                                        "accn": "0001585521-26-000100", "fy": 2026, "fp": "Q4",
                                        "form": "10-Q", "filed": "2026-02-20",
                                    },
                                    {
                                        "start": "2025-11-01", "end": "2026-01-31", "val": 111.0,
                                        "accn": "0001585521-26-000150", "fy": 2026, "fp": "Q4",
                                        "form": "10-Q/A", "filed": "2026-03-10",
                                    },
                                ]
                            }
                        }
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 4, 1))

            history = result.data.concepts["revenue"]
            self.assertEqual(len(history.datapoints), 1)
            self.assertEqual(history.datapoints[0].value, 111.0)
            self.assertEqual(history.datapoints[0].form, "10-Q/A")
            self.assertEqual(history.datapoints[0].filed, date(2026, 3, 10))

    def test_quarterly_duration_filter_excludes_annual_10k_entry(self) -> None:
        """A 10-K entry spanning a full fiscal year (~365 days) must not be treated as a
        quarterly datapoint for a flow concept — TTM must never silently ingest an annual tag."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2025-02-01", "end": "2026-01-31", "val": 4000.0,
                                        "accn": "0001585521-26-000200", "fy": 2026, "fp": "FY",
                                        "form": "10-K", "filed": "2026-03-15",
                                    },
                                    *_quarterly_entries("10-Q", 100.0, count=3),
                                ]
                            }
                        }
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 4, 1))

            history = result.data.concepts["revenue"]
            self.assertEqual(len(history.datapoints), 3)
            self.assertTrue(all(dp.value != 4000.0 for dp in history.datapoints))


class MarketableSecuritiesExtractionTests(TestCase):
    def test_short_and_long_term_investments_extracted_as_separate_concepts(self) -> None:
        """CashAndCashEquivalentsAtCarryingValue alone understates liquidity; short-term and
        long-term marketable securities must be extracted as their own named concepts."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "CashAndCashEquivalentsAtCarryingValue": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 890.9)]}
                        },
                        "MarketableSecuritiesCurrent": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 3200.0)]}
                        },
                        "MarketableSecuritiesNoncurrent": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 3609.1)]}
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            data = result.data
            self.assertAlmostEqual(data.concepts["cash_and_equivalents"].datapoints[0].value, 890.9)
            self.assertAlmostEqual(data.concepts["short_term_investments"].datapoints[0].value, 3200.0)
            self.assertAlmostEqual(data.concepts["long_term_investments"].datapoints[0].value, 3609.1)
            self.assertEqual(data.concepts["short_term_investments"].tag, "MarketableSecuritiesCurrent")

    def test_filer_reporting_under_available_for_sale_debt_securities_current_is_found(self) -> None:
        """Regression for the Zoom defect: the old candidate-tag list for
        short_term_investments was [ShortTermInvestments, MarketableSecuritiesCurrent,
        AvailableForSaleSecuritiesCurrent] and never looked for
        AvailableForSaleSecuritiesDebtSecuritiesCurrent at all, so a filer using that tag
        (Zoom holds $6.830B of marketable securities under it) silently came back as "no
        data" and the money vanished from total_liquid_assets. It must now be found and
        preferred as the first candidate."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "CashAndCashEquivalentsAtCarryingValue": {
                            "units": {"USD": [_instant_entry(date(2026, 4, 30), 891.0)]}
                        },
                        "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {
                            "units": {"USD": [_instant_entry(date(2026, 4, 30), 6830.0)]}
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 6, 1))

            data = result.data
            self.assertAlmostEqual(data.concepts["short_term_investments"].datapoints[0].value, 6830.0)
            self.assertEqual(
                data.concepts["short_term_investments"].tag, "AvailableForSaleSecuritiesDebtSecuritiesCurrent"
            )

    def test_long_term_investments_strategic_stake_reported_as_non_operating_assets(self) -> None:
        """Zoom's LongTermInvestments ($1.876B) is a strategic/venture equity-stake
        bucket (including its Anthropic position), not marketable securities. It must be
        extracted under the separate non_operating_assets concept (in addition to still
        being visible under the legacy long_term_investments concept), so the valuation
        layer can keep it out of total_liquid_assets."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "LongTermInvestments": {
                            "units": {"USD": [_instant_entry(date(2026, 4, 30), 1876.0)]}
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 6, 1))

            data = result.data
            self.assertAlmostEqual(data.concepts["non_operating_assets"].datapoints[0].value, 1876.0)
            self.assertEqual(data.concepts["non_operating_assets"].tag, "LongTermInvestments")

    def test_filer_with_no_marketable_securities_at_all_yields_no_concept(self) -> None:
        """A filer that simply has no short-term-investments-shaped tag anywhere in its
        companyfacts (e.g. a company that holds only cash) must come back with the
        concept absent, not an error and not a fabricated zero — exercising the
        None-with-reason path downstream in valuation.py."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "CashAndCashEquivalentsAtCarryingValue": {
                            "units": {"USD": [_instant_entry(date(2026, 4, 30), 42.0)]}
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 6, 1))

            data = result.data
            self.assertNotIn("short_term_investments", data.concepts)
            self.assertNotIn("long_term_investments", data.concepts)
            self.assertNotIn("non_operating_assets", data.concepts)


class StaleTagRecencyTests(TestCase):
    """Regression coverage for recency-aware multi-tag concept resolution (see
    sec.py's _STALE_TAG_WINDOW_DAYS / _select_tag_order): the first candidate tag with
    ANY data is no longer automatically the winner if a later-priority candidate is the
    one still actually current for this filer.
    """

    def test_stale_preferred_tag_is_skipped_for_a_fresher_alternative(self) -> None:
        """Reproduces the Apple defect: AvailableForSaleSecuritiesDebtSecuritiesCurrent
        is short_term_investments' first-priority candidate tag and is correct/current
        for some filers, but was abandoned by Apple in 2011. With a fresh revenue/cash
        anchor established, and MarketableSecuritiesCurrent actually current, the fresh
        candidate must win over the higher-priority but long-abandoned one."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {"USD": _quarterly_entries("10-Q", 900.0)}
                        },
                        "CashAndCashEquivalentsAtCarryingValue": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 39540.0)]}
                        },
                        "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {
                            "units": {
                                "USD": [_instant_entry(date(2011, 3, 26), 13260.0, accn="old-accn")]
                            }
                        },
                        "MarketableSecuritiesCurrent": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 22860.0)]}
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            history = result.data.concepts["short_term_investments"]
            self.assertEqual(history.tag, "MarketableSecuritiesCurrent")
            self.assertAlmostEqual(history.datapoints[0].value, 22860.0)

    def test_current_preferred_tag_is_unaffected_by_the_staleness_check(self) -> None:
        """Zoom regression guard: AvailableForSaleSecuritiesDebtSecuritiesCurrent is
        itself Zoom's freshest reporting tag. Adding revenue/cash data (which now feeds
        the staleness anchor) must not change which tag wins, and must not add any
        notes or data_gaps -- nothing here is stale."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {"USD": _quarterly_entries("10-Q", 1200.0)}
                        },
                        "CashAndCashEquivalentsAtCarryingValue": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 891.0)]}
                        },
                        "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 6830.0)]}
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            history = result.data.concepts["short_term_investments"]
            self.assertEqual(history.tag, "AvailableForSaleSecuritiesDebtSecuritiesCurrent")
            self.assertAlmostEqual(history.datapoints[0].value, 6830.0)
            self.assertEqual(result.data.notes, [])
            self.assertEqual(result.data.data_gaps, [])

    def test_all_candidates_stale_falls_back_and_records_it(self) -> None:
        """No short_term_investments candidate tag has data anywhere near this filer's
        current reporting period. Extraction must still return the best (highest
        priority) stale candidate rather than nothing, but must flag the staleness on
        ConceptHistory.tag, ValuationFundamentals.notes, and
        ValuationFundamentals.data_gaps -- a stale read must never be silent."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {"USD": _quarterly_entries("10-Q", 500.0)}
                        },
                        "CashAndCashEquivalentsAtCarryingValue": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 200.0)]}
                        },
                        "AvailableForSaleSecuritiesDebtSecuritiesCurrent": {
                            "units": {
                                "USD": [_instant_entry(date(2015, 6, 30), 300.0, accn="old1")]
                            }
                        },
                        "MarketableSecuritiesCurrent": {
                            "units": {
                                "USD": [_instant_entry(date(2016, 9, 30), 250.0, accn="old2")]
                            }
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            history = result.data.concepts["short_term_investments"]
            # Highest-priority candidate still wins among the (all-stale) group.
            self.assertTrue(history.tag.startswith("AvailableForSaleSecuritiesDebtSecuritiesCurrent"))
            self.assertIn("STALE FALLBACK", history.tag)
            self.assertAlmostEqual(history.datapoints[0].value, 300.0)

            self.assertTrue(any("short_term_investments" in note for note in result.data.notes))
            self.assertTrue(any("short_term_investments" in gap for gap in result.data.data_gaps))


class QuarterizationTests(TestCase):
    """Regression tests for the duration-fact normaliser (_quarterize_duration_entries /
    _extract_flow_concept_history) that derives a filer's missing standalone quarters
    from cumulative (year-to-date) facts via start/end date arithmetic."""

    def test_missing_q4_is_derived_from_fy_minus_nine_months(self) -> None:
        """Reproduces the exact Zoom revenue fact pattern: Q1/Q2/Q3 are reported
        standalone, but Q4 only exists implicitly inside the 10-K's full-year total. Q4
        must be derived as FY - 9M and marked as such, and the resulting 4-quarter TTM
        must equal the verified $4,933.1M figure (not skip a quarter)."""
        with tempfile.TemporaryDirectory() as tmp:
            entries = [
                # Q1 FY26, reported standalone.
                {
                    "start": "2025-02-01", "end": "2025-04-30", "val": 1174.7,
                    "accn": "a1", "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": "2025-05-15",
                },
                # H1 FY26 cumulative (used only to derive Q2).
                {
                    "start": "2025-02-01", "end": "2025-07-31", "val": 2391.9,
                    "accn": "a2", "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2025-08-15",
                },
                # Q2 FY26, reported standalone.
                {
                    "start": "2025-05-01", "end": "2025-07-31", "val": 1217.2,
                    "accn": "a2", "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2025-08-15",
                },
                # 9M FY26 cumulative (used to derive Q4 below).
                {
                    "start": "2025-02-01", "end": "2025-10-31", "val": 3621.8,
                    "accn": "a3", "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2025-11-15",
                },
                # Q3 FY26, reported standalone.
                {
                    "start": "2025-08-01", "end": "2025-10-31", "val": 1229.8,
                    "accn": "a3", "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2025-11-15",
                },
                # FY26 10-K total — no standalone Q4 fact exists anywhere in the payload.
                {
                    "start": "2025-02-01", "end": "2026-01-31", "val": 4868.8,
                    "accn": "a4", "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-03-15",
                },
                # Q1 FY27, reported standalone (most recent quarter).
                {
                    "start": "2026-02-01", "end": "2026-04-30", "val": 1239.0,
                    "accn": "a5", "fy": 2027, "fp": "Q1", "form": "10-Q", "filed": "2026-05-15",
                },
            ]
            payload = {
                "facts": {
                    "us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": entries}}},
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 6, 1))

            history = result.data.concepts["revenue"]
            by_end = {dp.end: dp for dp in history.datapoints}

            q4 = by_end[date(2026, 1, 31)]
            self.assertAlmostEqual(q4.value, 4868.8 - 3621.8, places=3)
            self.assertTrue(q4.derived)
            self.assertIsNotNone(q4.derived_from)
            self.assertIn("FY", q4.derived_from)
            self.assertIn("9M", q4.derived_from)

            # Reported quarters must stay reported, not get overwritten by any derivation.
            self.assertFalse(by_end[date(2025, 4, 30)].derived)
            self.assertFalse(by_end[date(2025, 7, 31)].derived)
            self.assertFalse(by_end[date(2025, 10, 31)].derived)
            self.assertFalse(by_end[date(2026, 4, 30)].derived)

            ttm_points = history.datapoints[:4]
            ttm = sum(dp.value for dp in ttm_points)
            self.assertAlmostEqual(ttm, 4933.1, delta=0.5)
            max_gap = max(
                (ttm_points[i].end - ttm_points[i + 1].end).days for i in range(len(ttm_points) - 1)
            )
            self.assertLessEqual(max_gap, 100)

    def test_ytd_cumulative_cashflow_concept_derives_discrete_quarters(self) -> None:
        """Cash-flow-statement concepts are frequently tagged as year-to-date cumulative
        in Q2/Q3 10-Qs ("six months ended", "nine months ended") rather than discrete —
        only the ~91-day Q1 entry used to survive the old duration-only filter, leaving
        one datapoint per year instead of four. Q2 and Q3 must now be derived via
        subtraction (Q2 = H1 - Q1, Q3 = 9M - H1) from exactly the same start/end
        mechanism used for revenue."""
        with tempfile.TemporaryDirectory() as tmp:
            entries = [
                # Q1, reported standalone (the only discrete quarter this filer reports).
                {
                    "start": "2025-02-01", "end": "2025-04-30", "val": 300.0,
                    "accn": "a1", "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": "2025-05-15",
                },
                # H1, tagged year-to-date cumulative (no standalone Q2 fact exists).
                {
                    "start": "2025-02-01", "end": "2025-07-31", "val": 640.0,
                    "accn": "a2", "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2025-08-15",
                },
                # 9M, tagged year-to-date cumulative (no standalone Q3 fact exists).
                {
                    "start": "2025-02-01", "end": "2025-10-31", "val": 1000.0,
                    "accn": "a3", "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2025-11-15",
                },
                # Q4 (next fiscal year's Q1, needed as the 4th most-recent quarter).
                {
                    "start": "2025-11-01", "end": "2026-01-31", "val": 360.0,
                    "accn": "a4", "fy": 2026, "fp": "Q4", "form": "10-K", "filed": "2026-03-15",
                },
            ]
            payload = {
                "facts": {
                    "us-gaap": {"NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": entries}}},
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 4, 1))

            history = result.data.concepts["operating_cashflow"]
            by_end = {dp.end: dp for dp in history.datapoints}

            q2 = by_end[date(2025, 7, 31)]
            self.assertAlmostEqual(q2.value, 640.0 - 300.0)
            self.assertTrue(q2.derived)

            q3 = by_end[date(2025, 10, 31)]
            self.assertAlmostEqual(q3.value, 1000.0 - 640.0)
            self.assertTrue(q3.derived)

            # The raw ~180-day and ~272-day cumulative facts must never appear as
            # datapoints themselves (only their derived discrete-quarter differences).
            self.assertTrue(all(dp.value not in (640.0, 1000.0) for dp in history.datapoints))


class DualClassShareCountTests(TestCase):
    def test_sums_classes_reported_in_the_same_filing(self) -> None:
        """Zoom-style dual-class cover page: Class A + Class B reported in the same accession
        (no dimension label in companyfacts) must be summed, not truncated to the first entry."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {},
                    "dei": {
                        "EntityCommonStockSharesOutstanding": {
                            "units": {
                                "shares": [
                                    # Older filing, single class value — must be excluded (superseded).
                                    {
                                        "end": "2025-09-01", "val": 260000000.0,
                                        "accn": "0001585521-25-000090", "form": "10-Q", "filed": "2025-09-05",
                                    },
                                    # Latest filing, two classes in the same accession.
                                    {
                                        "end": "2026-03-01", "val": 253000000.0,
                                        "accn": "0001585521-26-000120", "form": "10-Q", "filed": "2026-03-05",
                                    },
                                    {
                                        "end": "2026-03-01", "val": 40200000.0,
                                        "accn": "0001585521-26-000120", "form": "10-Q", "filed": "2026-03-05",
                                    },
                                ]
                            }
                        }
                    },
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 4, 1))

            data = result.data
            self.assertAlmostEqual(data.cover_page_shares, 293200000.0)
            self.assertEqual(len(data.cover_page_share_classes), 2)
            self.assertEqual(data.cover_page_meta["class_count"], 2)
            self.assertEqual(data.cover_page_meta["accn"], "0001585521-26-000120")

    def test_duplicate_identical_entries_in_same_accession_are_not_double_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {},
                    "dei": {
                        "EntityCommonStockSharesOutstanding": {
                            "units": {
                                "shares": [
                                    {
                                        "end": "2026-03-01", "val": 100000000.0,
                                        "accn": "0001-26-000001", "form": "10-Q", "filed": "2026-03-05",
                                    },
                                    {
                                        "end": "2026-03-01", "val": 100000000.0,
                                        "accn": "0001-26-000001", "form": "10-Q", "filed": "2026-03-05",
                                    },
                                ]
                            }
                        }
                    },
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 4, 1))

            self.assertAlmostEqual(result.data.cover_page_shares, 100000000.0)
            self.assertEqual(len(result.data.cover_page_share_classes), 1)


class GracefulDegradationTests(TestCase):
    def test_returns_none_when_no_recognized_concepts_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"facts": {"us-gaap": {"SomeUnrelatedTag": {"units": {"USD": []}}}, "dei": {}}}
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            self.assertIsNone(result.data)
            self.assertFalse(result.status.success)
            self.assertTrue(result.status.partial)

    def test_missing_concepts_simply_absent_from_dict_not_an_error(self) -> None:
        """Only revenue present; other concepts (gross_profit, operating_income, ...) must be
        absent from the `concepts` dict rather than raising or being silently defaulted."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {"Revenues": {"units": {"USD": _quarterly_entries("10-Q", 100.0)}}},
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            self.assertIn("revenue", result.data.concepts)
            self.assertNotIn("gross_profit", result.data.concepts)
            self.assertNotIn("operating_income", result.data.concepts)
            self.assertIsNone(result.data.cover_page_shares)

    def test_no_cik_mapping_returns_graceful_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            http = RoutingFakeHttpClient({SecClient.ticker_map_url: {"data": []}})
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("NOPE", date(2026, 3, 1))

            self.assertIsNone(result.data)
            self.assertFalse(result.status.success)
            self.assertIn("No CIK mapping", result.status.message)


class PayloadCacheReuseTests(TestCase):
    def test_company_facts_and_valuation_fundamentals_share_one_http_call(self) -> None:
        """SEC companyfacts already returns every tag in one request; calling both
        fetch_company_facts and fetch_valuation_fundamentals for the same (ticker, run_date)
        must not double the number of HTTP requests against data.sec.gov."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "Revenues": {"units": {"USD": _quarterly_entries("10-Q", 100.0)}},
                        "NetCashProvidedByUsedInOperatingActivities": {
                            "units": {"USD": _quarterly_entries("10-Q", 30.0)}
                        },
                    },
                    "dei": {},
                }
            }
            companyfacts_url = SecClient.companyfacts_url.format(cik="0001585521")
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    companyfacts_url: payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))
            run_date = date(2026, 3, 1)

            client.fetch_company_facts("ZM", run_date)
            client.fetch_valuation_fundamentals("ZM", run_date)

            self.assertEqual(http.call_counts.get(companyfacts_url), 1)


class BeneishConceptExtractionTests(TestCase):
    """Coverage for the three concepts added so beneish_m_score (valuation_models/_quality.py)
    can stop self-skipping: accounts_receivable, sga_expense, gross_ppe."""

    def test_accounts_receivable_extracted_from_first_candidate_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "AccountsReceivableNetCurrent": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 512.0)]}
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            history = result.data.concepts["accounts_receivable"]
            self.assertEqual(history.tag, "AccountsReceivableNetCurrent")
            self.assertAlmostEqual(history.datapoints[0].value, 512.0)

    def test_accounts_receivable_falls_back_through_candidate_tags(self) -> None:
        """Only the third candidate (AccountsReceivableGrossCurrent) is present; it must
        still be found rather than the concept coming back absent."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "AccountsReceivableGrossCurrent": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 480.0)]}
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            history = result.data.concepts["accounts_receivable"]
            self.assertEqual(history.tag, "AccountsReceivableGrossCurrent")
            self.assertAlmostEqual(history.datapoints[0].value, 480.0)

    def test_gross_ppe_uses_gross_tag_when_reported_with_no_fallback_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "PropertyPlantAndEquipmentGross": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 900.0)]}
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            history = result.data.concepts["gross_ppe"]
            self.assertEqual(history.tag, "PropertyPlantAndEquipmentGross")
            self.assertAlmostEqual(history.datapoints[0].value, 900.0)
            self.assertEqual(result.data.notes, [])

    def test_gross_ppe_falls_back_to_net_and_flags_it_in_notes(self) -> None:
        """Beneish's AQI/DEPI ratios assume GROSS PP&E; when only the net tag is
        reported, extraction must still succeed (net as a documented fallback) but the
        fallback must be visible both on the ConceptHistory.tag and via an explanatory
        note on ValuationFundamentals.notes -- never silently treated as equivalent to
        gross."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "PropertyPlantAndEquipmentNet": {
                            "units": {"USD": [_instant_entry(date(2026, 1, 31), 700.0)]}
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            history = result.data.concepts["gross_ppe"]
            self.assertEqual(history.tag, "PropertyPlantAndEquipmentNet")
            self.assertAlmostEqual(history.datapoints[0].value, 700.0)
            self.assertEqual(len(result.data.notes), 1)
            self.assertIn("gross_ppe", result.data.notes[0])
            self.assertIn("PropertyPlantAndEquipmentNet", result.data.notes[0])

    def test_sga_expense_extracted_from_combined_tag(self) -> None:
        """When a filer reports the combined SellingGeneralAndAdministrativeExpense tag,
        it must be used directly (quarterized like every other additive flow concept),
        with no split-tag fallback involved."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "SellingGeneralAndAdministrativeExpense": {
                            "units": {"USD": _quarterly_entries("10-Q", 200.0)}
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            history = result.data.concepts["sga_expense"]
            self.assertEqual(history.tag, "SellingGeneralAndAdministrativeExpense")
            self.assertEqual(len(history.datapoints), 5)

    def test_sga_expense_sums_split_tags_only_for_periods_both_report(self) -> None:
        """No combined tag exists; G&A and Selling are split. Only the periods where
        BOTH tags report data may be summed into sga_expense -- a period covered by
        just one of the two tags must be dropped rather than treated as if it were the
        whole of SG&A (which would silently understate that quarter and corrupt SGAI)."""
        with tempfile.TemporaryDirectory() as tmp:
            shared_period = {"start": "2025-11-01", "end": "2026-01-31"}
            ga_only_period = {"start": "2025-08-01", "end": "2025-10-31"}
            payload = {
                "facts": {
                    "us-gaap": {
                        "GeneralAndAdministrativeExpense": {
                            "units": {
                                "USD": [
                                    {
                                        **shared_period, "val": 120.0, "accn": "a1", "fy": 2026,
                                        "fp": "Q1", "form": "10-Q", "filed": "2026-02-20",
                                    },
                                    {
                                        **ga_only_period, "val": 90.0, "accn": "a0", "fy": 2025,
                                        "fp": "Q4", "form": "10-Q", "filed": "2025-11-20",
                                    },
                                ]
                            }
                        },
                        "SellingAndMarketingExpense": {
                            "units": {
                                "USD": [
                                    {
                                        **shared_period, "val": 80.0, "accn": "a1", "fy": 2026,
                                        "fp": "Q1", "form": "10-Q", "filed": "2026-02-20",
                                    },
                                ]
                            }
                        },
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            history = result.data.concepts["sga_expense"]
            self.assertIn("summed", history.tag)
            self.assertIn("GeneralAndAdministrativeExpense", history.tag)
            self.assertIn("SellingAndMarketingExpense", history.tag)

            by_end = {dp.end: dp for dp in history.datapoints}
            self.assertIn(date(2026, 1, 31), by_end)
            self.assertAlmostEqual(by_end[date(2026, 1, 31)].value, 200.0)

            # The G&A-only period must not appear as a (wrong, understated) datapoint.
            self.assertNotIn(date(2025, 10, 31), by_end)
            self.assertTrue(all(dp.value != 90.0 for dp in history.datapoints))

    def test_sga_expense_absent_when_only_one_split_tag_exists(self) -> None:
        """Only GeneralAndAdministrativeExpense is reported anywhere -- no combined tag,
        no SellingAndMarketingExpense counterpart to sum against. sga_expense must come
        back absent, never silently defined as "G&A alone"."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "facts": {
                    "us-gaap": {
                        "GeneralAndAdministrativeExpense": {
                            "units": {"USD": _quarterly_entries("10-Q", 90.0)}
                        },
                        # Unrelated concept present so the payload yields a non-None
                        # ValuationFundamentals to assert against.
                        "Revenues": {"units": {"USD": _quarterly_entries("10-Q", 100.0)}},
                    },
                    "dei": {},
                }
            }
            http = RoutingFakeHttpClient(
                {
                    SecClient.ticker_map_url: _ticker_map_payload(),
                    SecClient.companyfacts_url.format(cik="0001585521"): payload,
                }
            )
            client = SecClient(http, _make_storage(tmp))

            result = client.fetch_valuation_fundamentals("ZM", date(2026, 3, 1))

            self.assertNotIn("sga_expense", result.data.concepts)
