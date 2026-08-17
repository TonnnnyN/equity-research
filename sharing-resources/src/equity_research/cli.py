from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from equity_research.config import load_config
from equity_research.models import Layer
from equity_research.pipeline import DailyPipeline
from equity_research import valuation_models
from equity_research.valuation_models._types import ModelOrder, DeclinedModel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="equity-research")
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Optional path to the project TOML config.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Initialize the SQLite database.")

    show_report = subparsers.add_parser("show-report", help="Display a saved markdown report.")
    show_report.add_argument("--date", dest="run_date", required=True, help="Run date in YYYY-MM-DD format.")

    subparsers.add_parser("preflight", help="Validate runtime configuration for optional providers.")

    cleanup_data = subparsers.add_parser("cleanup-data", help="Prune old reports, raw payloads, and cached DB rows.")
    cleanup_data.add_argument(
        "--date",
        dest="run_date",
        help="Reference date in YYYY-MM-DD format. Defaults to today in the configured timezone.",
    )

    review = subparsers.add_parser(
        "review",
        help="Run a deep-review pass on one or more tickers and emit review packets.",
    )
    review.add_argument(
        "tickers",
        nargs="+",
        help="Ticker symbols to review (e.g. ZM NVDA AMZN).",
    )
    review.add_argument(
        "--benchmark",
        dest="benchmark",
        default=None,
        help="Benchmark ticker for relative performance calculation (default: QQQ if not specified).",
    )
    review.add_argument(
        "--layer",
        dest="layer",
        default=None,
        choices=[layer.value for layer in Layer],
        help="Layer to use when looking up trigger thresholds. If not specified, generic thresholds are used.",
    )
    review.add_argument(
        "--date",
        dest="run_date",
        help="Run date in YYYY-MM-DD format. Defaults to today.",
    )

    review_ticker = subparsers.add_parser(
        "review-ticker",
        help="Run a deep-review pass on a single ad-hoc ticker and emit a review packet.",
    )
    review_ticker.add_argument(
        "ticker",
        help="The ticker symbol to review (e.g. ACMR, IRTC).",
    )
    review_ticker.add_argument(
        "--benchmark",
        dest="benchmark",
        default=None,
        help="Benchmark ticker for relative performance calculation (default: QQQ if not specified).",
    )
    review_ticker.add_argument(
        "--layer",
        dest="layer",
        default=None,
        choices=[layer.value for layer in Layer],
        help="Layer to use when looking up trigger thresholds. If not specified, generic thresholds are used.",
    )
    review_ticker.add_argument(
        "--date",
        dest="run_date",
        help="Run date in YYYY-MM-DD format. Defaults to today.",
    )
    review_ticker.add_argument(
        "--name",
        dest="name",
        default=None,
        help="Optional human-readable company name.",
    )

    valuation_order = subparsers.add_parser(
        "valuation-order",
        help="Place a valuation model order and run ordered models for a ticker.",
    )
    valuation_order.add_argument(
        "ticker",
        help="The ticker symbol to run models for (e.g. ACMR, IRTC, ZM).",
    )
    valuation_order.add_argument(
        "--order",
        dest="order_file",
        required=True,
        help="Path to the order JSON file (models, assumptions, rationale, declined).",
    )
    valuation_order.add_argument(
        "--date",
        dest="run_date",
        help="Run date in YYYY-MM-DD format. Defaults to today.",
    )
    valuation_order.add_argument(
        "--portrait",
        dest="portrait_file",
        default=None,
        help="Optional path to company portrait JSON file to store alongside the order.",
    )

    check_decisions = subparsers.add_parser(
        "check-decisions",
        help="Evaluate open decisions against fresh prices and report status.",
    )
    check_decisions.add_argument(
        "--date",
        dest="run_date",
        help="Check date in YYYY-MM-DD format. Defaults to today.",
    )

    return parser


def resolve_run_date(run_date: str | None, timezone_name: str, now: datetime | None = None) -> date:
    if run_date:
        return datetime.strptime(run_date, "%Y-%m-%d").date()

    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return date.today()
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    return current.astimezone(zone).date()


def _handle_valuation_order(pipeline: DailyPipeline, args: argparse.Namespace, run_date: date) -> int:
    """Handle the valuation-order subcommand.

    Loads an order JSON, fetches valuation fundamentals/derived/prices for the ticker,
    runs the ordered models, and writes the report to disk.
    """
    from equity_research.valuation import compute_valuation_derived

    ticker = args.ticker.upper()
    order_file = Path(args.order_file)
    portrait_file = Path(args.portrait_file) if args.portrait_file else None

    # Load and parse the order JSON
    try:
        order_data = json.loads(order_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Failed to load order file {order_file}: {exc}", file=sys.stderr)
        return 1

    # Build the ModelOrder, allowing ValueError to propagate for loud failure
    try:
        # Convert lists to tuples as required by ModelOrder
        models = tuple(order_data.get("models", []))
        assumptions = order_data.get("assumptions", {})
        rationale = order_data.get("rationale", "")
        declined_list = order_data.get("declined", [])
        declined = tuple(
            DeclinedModel(model=d["model"], reason=d["reason"])
            for d in declined_list
        )
        order = ModelOrder(models=models, assumptions=assumptions, rationale=rationale, declined=declined)
    except (KeyError, ValueError) as exc:
        print(f"Invalid order JSON: {exc}", file=sys.stderr)
        return 1

    # Fetch valuation data for the ticker
    try:
        pipeline.storage.init_db()

        # Fetch prices
        prices, _, _ = pipeline.get_price_history(ticker, run_date)
        if not prices:
            print(f"Could not fetch price history for {ticker}", file=sys.stderr)
            return 1

        # Fetch valuation fundamentals and derive valuation inputs
        valuation_payload = pipeline._fetch_valuation_fundamentals_with_recovery(ticker, run_date)
        valuation_fundamentals = valuation_payload.data
        if valuation_fundamentals is None:
            print(f"Could not fetch valuation fundamentals for {ticker}", file=sys.stderr)
            return 1

        # Compute valuation derived metrics
        benchmark_prices, _, _ = pipeline.get_price_history("IWM", run_date)
        valuation_derived = compute_valuation_derived(
            ticker=ticker,
            as_of=run_date,
            fundamentals=valuation_fundamentals,
            prices=prices,
            benchmark_prices=benchmark_prices,
        )
        if valuation_derived is None:
            print(f"Could not compute valuation derived metrics for {ticker}", file=sys.stderr)
            return 1

        # Create a minimal Security object
        from equity_research.models import Security
        security = Security(
            ticker=ticker,
            name=ticker,
            layer="ai_applications",
            benchmark="IWM",
        )

    except Exception as exc:
        print(f"Failed to fetch valuation data for {ticker}: {exc}", file=sys.stderr)
        return 1

    # Run the order through the valuation models (loud failure for invalid orders)
    try:
        report = valuation_models.run_order(
            security=security,
            order=order,
            derived=valuation_derived,
            fundamentals=valuation_fundamentals,
            prices=prices,
            peer_contexts=[],
        )
    except ValueError as exc:
        # Loud failure for invalid orders — agent mistake
        print(f"Invalid valuation order: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Failed to run valuation models: {exc}", file=sys.stderr)
        return 1

    # Write the report to disk
    report_dir = pipeline.storage.data_dir / "reports" / run_date.isoformat()
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{ticker}_valuation_report.json"
    report_json = json.dumps(report.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
    report_path.write_text(report_json, encoding="utf-8")

    # Store the order in the database with derivation metadata
    order_with_metadata = {
        **order_data,
        "_derivation_type": "full",
        "_derivation_date": run_date.isoformat(),
    }
    if prices:
        order_with_metadata["_reference_close"] = prices[-1].close
    pipeline.storage.upsert_model_order(
        ticker, run_date, json.dumps(order_with_metadata, ensure_ascii=False), str(report_path)
    )

    # Store the portrait if provided
    if portrait_file and portrait_file.exists():
        try:
            portrait_text = portrait_file.read_text(encoding="utf-8")
            # Parse the portrait, add derivation metadata, and re-serialize
            portrait_obj = json.loads(portrait_text)
            portrait_obj["_derivation_type"] = "full"
            portrait_obj["_derivation_date"] = run_date.isoformat()
            # Extract reference close from prices (most recent price)
            if prices:
                reference_close = prices[-1].close
                portrait_obj["_reference_close"] = reference_close
            portrait_data = json.dumps(portrait_obj, ensure_ascii=False)
            pipeline.storage.upsert_company_portrait(ticker, run_date, portrait_data)
            print(f"[valuation-order] portrait stored for {ticker}", file=sys.stderr)
        except Exception as exc:
            print(f"Warning: failed to store portrait: {exc}", file=sys.stderr)

    # Print readable summary
    print(f"Valuation report for {ticker} on {run_date}:")
    print(f"  Models run: {len(report.results)}")
    print(f"  Always-on metrics: {len(report.always_on)}")
    print(f"  Report saved to: {report_path}")
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))

    return 0


def _handle_check_decisions(pipeline: DailyPipeline, run_date: date) -> int:
    """Handle the check-decisions subcommand.

    Evaluate open decisions against fresh prices (cheap: no SEC, no valuation).
    Print a readable summary of what fired and what is still open.
    """
    pipeline.storage.init_db()

    # Run the decision tracking pass
    result = pipeline._track_decisions(run_date)

    # Print any warnings from ingestion
    if result.ingest_warnings:
        print("[warnings]", file=sys.stderr)
        for warning in result.ingest_warnings:
            print(f"  {warning}", file=sys.stderr)
        print()

    # Print fired alerts
    if result.alerts:
        print(f"[fired alerts: {len(result.alerts)}]")
        for alert in result.alerts:
            print(f"  {alert.ticker} {alert.decision_date}: {alert.kind} - {alert.reason}")
        print()

    # Print still-active decisions
    if result.active_summaries:
        print(f"[still open: {len(result.active_summaries)}]")
        for summary in result.active_summaries:
            ticker = summary["ticker"]
            state = summary["state"]
            decision_date = summary["decision_date"]
            invalidate_conds = ", ".join(summary["invalidate_conditions"]) if summary["invalidate_conditions"] else "none"
            print(f"  {ticker} {decision_date} ({state}): invalidate_if=[{invalidate_conds}]")
        print()

    # Print compact status line
    status_line = f"ledger: {len(result.active_summaries)} open, {len(result.alerts)} fired"
    print(status_line)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pipeline = DailyPipeline(config=load_config(args.config_path) if args.config_path else None)

    if args.command == "init-db":
        pipeline.init_db()
        print(f"Initialized database at {pipeline.storage.db_path}")
        return 0

    run_date = resolve_run_date(getattr(args, "run_date", None), pipeline.config.timezone)
    if args.command == "review":
        layer = Layer(args.layer) if args.layer else None
        benchmark = args.benchmark.upper() if args.benchmark else None
        for ticker in args.tickers:
            ticker = ticker.upper()
            full_packet = pipeline.review_single(
                ticker,
                benchmark=benchmark,
                layer=layer,
                run_date=run_date,
                name=None,
            )

            # Extract Tier 2 (working packet) from Tier 3 (full packet)
            from equity_research.storage import _extract_tier2_from_full
            tier2_packet = _extract_tier2_from_full(full_packet)

            # Print Tier 2 (this is what agents read by default)
            packet_json = json.dumps(tier2_packet, indent=2, sort_keys=True)
            print(packet_json)

            # Write both Tier 2 and Tier 3 to disk.
            out_dir = pipeline.storage.data_dir / "reports" / run_date.isoformat() / "review_packets"
            out_dir.mkdir(parents=True, exist_ok=True)

            # Save Tier 2 (default load, ~32 KB)
            tier2_path = out_dir / f"{ticker}.json"
            tier2_path.write_text(packet_json, encoding="utf-8")
            print(f"\n[review] Tier 2 packet saved to {tier2_path}", file=sys.stderr)

            # Save Tier 3 (full, ~288 KB, available for reference)
            full_json = json.dumps(full_packet, indent=2, sort_keys=True)
            full_path = out_dir / f"{ticker}.full.json"
            full_path.write_text(full_json, encoding="utf-8")
            print(f"[review] Tier 3 packet saved to {full_path}", file=sys.stderr)

        return 0

    if args.command == "show-report":
        print(pipeline.storage.load_delivery_report_markdown(run_date))
        return 0

    if args.command == "preflight":
        summary = pipeline.preflight()
        print("\n".join(summary.to_lines()))
        return 0 if summary.ready else 1

    if args.command == "cleanup-data":
        retention = pipeline.config.retention
        summary = pipeline.storage.cleanup_retention(
            reference_date=run_date,
            report_days=retention.report_days,
            raw_payload_days=retention.raw_payload_days,
            daily_price_days=retention.daily_price_days,
            official_event_days=retention.official_event_days,
            fundamental_days=retention.fundamental_days,
            macro_days=retention.macro_days,
            run_metadata_days=retention.run_metadata_days,
            social_raw_payload_days=retention.social_raw_payload_days,
            social_post_days=retention.social_post_days,
            social_snapshot_days=retention.social_snapshot_days,
        )
        print("\n".join(summary.to_lines()))
        return 0

    if args.command == "review-ticker":
        ticker = args.ticker.upper()
        layer = Layer(args.layer) if args.layer else None
        benchmark = args.benchmark.upper() if args.benchmark else None
        full_packet = pipeline.review_single(
            ticker,
            benchmark=benchmark,
            layer=layer,
            run_date=run_date,
            name=getattr(args, "name", None),
        )

        # Extract Tier 2 (working packet) from Tier 3 (full packet)
        from equity_research.storage import _extract_tier2_from_full
        tier2_packet = _extract_tier2_from_full(full_packet)

        # Print Tier 2 (this is what agents read by default)
        packet_json = json.dumps(tier2_packet, indent=2, sort_keys=True)
        print(packet_json)

        # Write both Tier 2 and Tier 3 to disk.
        out_dir = pipeline.storage.data_dir / "reports" / run_date.isoformat() / "review_packets"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Save Tier 2 (default load, ~32 KB)
        tier2_path = out_dir / f"{ticker}.json"
        tier2_path.write_text(packet_json, encoding="utf-8")
        print(f"\n[review-ticker] Tier 2 packet saved to {tier2_path}", file=sys.stderr)

        # Save Tier 3 (full, ~288 KB, available for reference)
        full_json = json.dumps(full_packet, indent=2, sort_keys=True)
        full_path = out_dir / f"{ticker}.full.json"
        full_path.write_text(full_json, encoding="utf-8")
        print(f"[review-ticker] Tier 3 packet saved to {full_path}", file=sys.stderr)

        return 0

    if args.command == "valuation-order":
        return _handle_valuation_order(pipeline, args, run_date)

    if args.command == "check-decisions":
        return _handle_check_decisions(pipeline, run_date)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
