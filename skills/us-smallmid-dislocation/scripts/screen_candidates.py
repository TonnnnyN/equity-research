#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


HARD_FLAGS = {
    "going_concern",
    "default",
    "delisting",
    "fraud",
    "restatement",
    "material_weakness",
    "bankruptcy",
}

WATCHLIST_CEILING_FLAGS = {
    "guidance_cut",
    "dilution_risk",
    "secondary_offering",
    "refinancing_pressure",
    "exchange_compliance_notice",
    "late_filing",
}

SPECIAL_SITUATIONS = {
    "recent_despac",
    "reverse_split",
    "pre_revenue_biotech",
    "precommercial_medtech",
    "strategic_review",
    "busted_deal",
}

GOOD_EARNINGS_QUALITY = {"good", "decent", "acceptable", "strong"}
WEAK_TAPE_LABELS = {"flat", "weak", "negative", "bad", "down"}


@dataclass(slots=True)
class ScreenResult:
    ticker: str
    name: str
    state: str
    priority: str | None
    score: int
    benchmark: str
    trigger_reasons: list[str]
    stability_reasons: list[str]
    ceiling_reasons: list[str]
    flags: list[str]
    notes: list[str]
    market_cap_usd: float | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Screen a prepared U.S. small/mid-cap universe CSV for dislocation candidates.",
    )
    parser.add_argument("input_csv", help="CSV matching references/input_schema.md")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "defaults" / "universe.toml"),
        help="Path to the universe defaults TOML.",
    )
    parser.add_argument("--output-json", help="Optional path for JSON results.")
    parser.add_argument("--output-markdown", help="Optional path for markdown report.")
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum number of ranked rows to include in the markdown output.",
    )
    return parser.parse_args()


def load_config(path: str) -> dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def normalize_text(value: str | None) -> str:
    return (value or "").strip().lower().replace("-", "_").replace(" ", "_")


def split_values(value: str | None) -> set[str]:
    if not value:
        return set()
    normalized = value.replace("|", ",").replace(";", ",")
    return {normalize_text(item) for item in normalized.split(",") if item.strip()}


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_ratio(value: str | None) -> float | None:
    number = parse_float(value)
    if number is None:
        return None
    if abs(number) > 1:
        return number / 100.0
    return number


def parse_int(value: str | None) -> int | None:
    number = parse_float(value)
    if number is None:
        return None
    return int(number)


def first_present(row: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        if key in row and row[key].strip():
            return row[key]
    return None


def inferred_benchmark(market_cap_usd: float | None, row: dict[str, str]) -> str:
    explicit = first_present(row, "benchmark", "benchmark_ticker", "sector_benchmark")
    if explicit:
        return explicit.strip().upper()
    sector = first_present(row, "sector_etf", "sector_benchmark_ticker")
    if sector:
        return sector.strip().upper()
    if market_cap_usd is None:
        return "IWM"
    return "IJH" if market_cap_usd >= 8_000_000_000 else "IWM"


def passes_universe_filters(row: dict[str, str], config: dict) -> tuple[bool, list[str]]:
    filters = config["filters"]
    listing = config["listing"]
    reasons: list[str] = []
    exchange = (first_present(row, "exchange") or "").strip()
    country = (first_present(row, "country") or "US").strip().upper()
    security_type = normalize_text(first_present(row, "security_type", "asset_type"))
    structure_tokens = split_values(first_present(row, "structure", "security_tags"))
    industry_tokens = split_values(first_present(row, "industry", "industry_group"))
    sector_tokens = split_values(first_present(row, "sector"))
    flag_tokens = split_values(first_present(row, "flags", "special_situations"))
    market_cap = parse_float(first_present(row, "market_cap_usd"))
    price = parse_float(first_present(row, "price"))
    adv20 = parse_float(first_present(row, "avg_dollar_volume_20d_usd", "adv20_usd"))

    allowed_exchanges = set(listing["allowed_exchanges"])
    if exchange and exchange not in allowed_exchanges:
        reasons.append("exchange_not_allowed")
    if country != listing["country"]:
        reasons.append("country_not_allowed")
    if security_type and security_type not in {"common_stock", "common"}:
        reasons.append("security_type_not_common")
    if structure_tokens & set(filters["excluded_structures"]):
        reasons.append("excluded_structure")
    if industry_tokens & set(filters["excluded_industries"]):
        reasons.append("excluded_industry")
    if sector_tokens & set(filters["excluded_industries"]):
        reasons.append("excluded_sector")
    if flag_tokens & set(filters["excluded_special_situations"]):
        reasons.append("excluded_special_situation")
    if price is None or price < filters["min_price_usd"]:
        reasons.append("price_floor_failed")
    if market_cap is None or market_cap < filters["market_cap_min_usd"] or market_cap > filters["market_cap_max_usd"]:
        reasons.append("market_cap_out_of_range")
    if adv20 is None or adv20 < filters["min_avg_dollar_volume_20d_usd"]:
        reasons.append("liquidity_floor_failed")
    return (not reasons, reasons)


def evaluate_row(row: dict[str, str], config: dict) -> ScreenResult | None:
    passes_filters, _ = passes_universe_filters(row, config)
    if not passes_filters:
        return None

    ticker = (first_present(row, "ticker") or "UNKNOWN").strip().upper()
    name = (first_present(row, "name", "company_name") or ticker).strip()
    market_cap = parse_float(first_present(row, "market_cap_usd"))
    benchmark = inferred_benchmark(market_cap, row)
    flags = split_values(first_present(row, "flags", "special_situations"))

    dd52 = parse_ratio(first_present(row, "drawdown_52w", "drawdown_from_52w_high"))
    dd60 = parse_ratio(first_present(row, "drawdown_60d"))
    rel60 = parse_ratio(first_present(row, "relative_underperformance_60d", "rel_perf_60d"))
    earnings_days = parse_int(first_present(row, "earnings_days_ago", "days_since_earnings"))
    earnings_quality = normalize_text(first_present(row, "earnings_quality"))
    tape_label = normalize_text(first_present(row, "earnings_price_confirmation", "post_earnings_tape"))
    post_earn_rel = parse_ratio(first_present(row, "post_earnings_relative_return_5d"))

    trigger_reasons: list[str] = []
    notes: list[str] = []
    primary_trigger = False
    confirming_trigger = False

    if dd52 is not None and dd52 >= 0.35:
        primary_trigger = True
        trigger_reasons.append("52w_drawdown")
    if dd60 is not None and dd60 >= 0.25:
        primary_trigger = True
        trigger_reasons.append("60d_drawdown")
    if rel60 is not None and rel60 >= 0.15:
        confirming_trigger = True
        trigger_reasons.append("relative_underperformance")

    has_negative_event_exclusion = bool(flags & {"guidance_cut", "financing_warning", "major_negative_8k"})
    good_earnings_bad_tape = (
        earnings_days is not None
        and earnings_days <= 45
        and earnings_quality in GOOD_EARNINGS_QUALITY
        and not has_negative_event_exclusion
        and (
            tape_label in WEAK_TAPE_LABELS
            or (post_earn_rel is not None and post_earn_rel <= -0.05)
        )
    )
    if good_earnings_bad_tape:
        confirming_trigger = True
        trigger_reasons.append("good_earnings_bad_tape")

    revenue_yoy = parse_ratio(first_present(row, "revenue_yoy", "revenue_growth_yoy"))
    ocf = parse_float(first_present(row, "operating_cashflow_latest", "ocf_latest"))
    normalized_fcf = parse_float(first_present(row, "normalized_fcf_latest", "fcf_latest"))
    cash = parse_float(first_present(row, "cash_latest", "cash"))
    debt = parse_float(first_present(row, "debt_latest", "debt"))
    runway_months = parse_float(first_present(row, "runway_months", "cash_runway_months"))
    interest_coverage = parse_float(first_present(row, "interest_coverage"))
    filing_age_days = parse_int(first_present(row, "filing_age_days"))
    share_count_yoy = parse_ratio(first_present(row, "share_count_growth_yoy"))
    working_cap_release_ratio = parse_ratio(first_present(row, "working_capital_release_ratio"))

    stability_reasons: list[str] = []
    if revenue_yoy is not None and revenue_yoy > -0.05:
        stability_reasons.append("revenue_not_collapsing")
    if ocf is not None and ocf > 0:
        stability_reasons.append("positive_ocf")
    if normalized_fcf is not None and normalized_fcf > 0:
        stability_reasons.append("positive_normalized_fcf")
    if cash is not None and debt is not None and (debt <= 0 or cash >= 0.3 * debt):
        stability_reasons.append("cash_vs_debt_ok")
    if runway_months is not None and runway_months >= 12:
        stability_reasons.append("runway_ge_12m")
    if interest_coverage is not None and interest_coverage >= 2.0:
        stability_reasons.append("interest_coverage_ok")
    if share_count_yoy is not None and share_count_yoy <= 0.08:
        stability_reasons.append("share_count_stable")
    if filing_age_days is not None and filing_age_days <= 120:
        stability_reasons.append("filings_current")

    ceiling_reasons: list[str] = []
    hard_flags = sorted(flags & HARD_FLAGS)
    if hard_flags:
        ceiling_reasons.extend(hard_flags)
    special_flags = sorted(flags & SPECIAL_SITUATIONS)
    if special_flags:
        ceiling_reasons.extend([f"special:{flag}" for flag in special_flags])
    watchlist_ceiling_flags = sorted(flags & WATCHLIST_CEILING_FLAGS)
    if watchlist_ceiling_flags:
        ceiling_reasons.extend([f"ceiling:{flag}" for flag in watchlist_ceiling_flags])
    if runway_months is not None and runway_months < 12:
        ceiling_reasons.append("ceiling:runway_lt_12m")
    if filing_age_days is not None and filing_age_days > 120:
        ceiling_reasons.append("ceiling:stale_filings")
    if working_cap_release_ratio is not None and working_cap_release_ratio > 0.50:
        ceiling_reasons.append("ceiling:working_cap_release")
    if share_count_yoy is not None and share_count_yoy > 0.08:
        ceiling_reasons.append("ceiling:share_count_growth")

    score = 0
    if primary_trigger:
        score += 3
    if confirming_trigger:
        score += 3
    score += min(5, len(stability_reasons))
    score -= min(4, len(watchlist_ceiling_flags))
    score -= len(hard_flags) * 3
    score -= len(special_flags) * 2

    state = "Pass"
    priority: str | None = None
    if hard_flags or special_flags:
        state = "Pass"
        notes.append("hard_or_special_situation")
    elif not primary_trigger or not confirming_trigger:
        state = "Pass"
        notes.append("missing_cross_family_trigger")
    else:
        has_watchlist_ceiling = any(reason.startswith("ceiling:") for reason in ceiling_reasons)
        if len(stability_reasons) >= 5 and not has_watchlist_ceiling:
            state = "Investigate"
        elif len(stability_reasons) >= 3:
            state = "Watchlist"
        else:
            state = "Pass"
            notes.append("stability_bar_not_cleared")

    if state == "Investigate":
        priority = "A" if score >= 10 else "B"
    elif state == "Watchlist":
        priority = "B" if score >= 7 else "C"

    if state == "Investigate" and any(reason.startswith("ceiling:") for reason in ceiling_reasons):
        state = "Watchlist"
        priority = "B"

    return ScreenResult(
        ticker=ticker,
        name=name,
        state=state,
        priority=priority,
        score=score,
        benchmark=benchmark,
        trigger_reasons=trigger_reasons,
        stability_reasons=stability_reasons,
        ceiling_reasons=ceiling_reasons,
        flags=sorted(flags),
        notes=notes,
        market_cap_usd=market_cap,
    )


def render_markdown(results: list[ScreenResult], config: dict, limit: int) -> str:
    included = sorted(
        [result for result in results if result.state != "Pass"],
        key=lambda item: (item.state != "Investigate", -item.score, item.ticker),
    )
    counts = Counter(result.state for result in results)
    lines = [
        "# US Small/Mid Dislocation Screen",
        "",
        "## Summary",
        "",
        f"- Universe target: {config['universe']['target_count_nominal']}",
        f"- Screened names: {len(results)}",
        f"- Investigate: {counts.get('Investigate', 0)}",
        f"- Watchlist: {counts.get('Watchlist', 0)}",
        f"- Pass: {counts.get('Pass', 0)}",
        "",
        "## Ranked Candidates",
        "",
        "| Ticker | State | Priority | Score | Benchmark | Trigger | Stability | Ceilings |",
        "| --- | --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for result in included[:limit]:
        lines.append(
            "| {ticker} | {state} | {priority} | {score} | {benchmark} | {trigger} | {stability} | {ceilings} |".format(
                ticker=result.ticker,
                state=result.state,
                priority=result.priority or "",
                score=result.score,
                benchmark=result.benchmark,
                trigger=", ".join(result.trigger_reasons) or "-",
                stability=", ".join(result.stability_reasons[:3]) or "-",
                ceilings=", ".join(result.ceiling_reasons) or "-",
            )
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    results: list[ScreenResult] = []
    with open(args.input_csv, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            result = evaluate_row(row, config)
            if result is not None:
                results.append(result)

    results.sort(key=lambda item: (item.state != "Investigate", item.state != "Watchlist", -item.score, item.ticker))

    if args.output_json:
        payload = [
            {
                "ticker": result.ticker,
                "name": result.name,
                "state": result.state,
                "priority": result.priority,
                "score": result.score,
                "benchmark": result.benchmark,
                "trigger_reasons": result.trigger_reasons,
                "stability_reasons": result.stability_reasons,
                "ceiling_reasons": result.ceiling_reasons,
                "flags": result.flags,
                "notes": result.notes,
                "market_cap_usd": result.market_cap_usd,
            }
            for result in results
        ]
        Path(args.output_json).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    markdown = render_markdown(results, config, args.limit)
    if args.output_markdown:
        Path(args.output_markdown).write_text(markdown, encoding="utf-8")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
