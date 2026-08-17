from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from market_sentiment.config import load_config
from market_sentiment.email_delivery import send_report_email
from market_sentiment.models import Layer
from market_sentiment.pipeline import DailyPipeline
from market_sentiment import valuation_models
from market_sentiment.valuation_models._types import ModelOrder, DeclinedModel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-sentiment")
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Optional path to the project TOML config.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Initialize the SQLite database.")

    run_daily = subparsers.add_parser("run-daily", help="Run the daily pipeline.")
    run_daily.add_argument(
        "--date",
        dest="run_date",
        help="Run date in YYYY-MM-DD format. Defaults to today.",
    )
    run_daily.add_argument(
        "--email",
        dest="send_email",
        action="store_true",
        help="Send the generated report by email after the run completes.",
    )

    show_report = subparsers.add_parser("show-report", help="Display a saved markdown report.")
    show_report.add_argument("--date", dest="run_date", required=True, help="Run date in YYYY-MM-DD format.")

    subparsers.add_parser("preflight", help="Validate runtime configuration for optional providers.")

    cleanup_data = subparsers.add_parser("cleanup-data", help="Prune old reports, raw payloads, and cached DB rows.")
    cleanup_data.add_argument(
        "--date",
        dest="run_date",
        help="Reference date in YYYY-MM-DD format. Defaults to today in the configured timezone.",
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
        default="IWM",
        help="Benchmark ticker for relative performance calculation (default: IWM).",
    )
    review_ticker.add_argument(
        "--layer",
        dest="layer",
        default="ai_applications",
        choices=[layer.value for layer in Layer],
        help="Layer to use when looking up trigger thresholds (default: ai_applications).",
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
    from market_sentiment.valuation import compute_valuation_derived

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
        from market_sentiment.models import Security
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

    # Store the order in the database
    pipeline.storage.upsert_model_order(
        ticker, run_date, json.dumps(order_data), str(report_path)
    )

    # Store the portrait if provided
    if portrait_file and portrait_file.exists():
        try:
            portrait_data = portrait_file.read_text(encoding="utf-8")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pipeline = DailyPipeline(config=load_config(args.config_path) if args.config_path else None)

    if args.command == "init-db":
        pipeline.init_db()
        print(f"Initialized database at {pipeline.storage.db_path}")
        return 0

    run_date = resolve_run_date(getattr(args, "run_date", None), pipeline.config.timezone)
    if args.command == "run-daily":
        report = pipeline.run(run_date)
        if getattr(args, "send_email", False):
            email_settings = pipeline.config.report_email
            if not email_settings.is_configured():
                print(
                    "Email delivery requested, but SMTP settings are not configured.",
                    file=sys.stderr,
                )
                return 2
            try:
                report_markdown = pipeline.storage.load_delivery_report_markdown(run_date)
                send_report_email(report, report_markdown, email_settings)
            except Exception as exc:
                print(f"Failed to send report email: {exc}", file=sys.stderr)
                return 1
        print(f"Completed run for {report.run_date.isoformat()} with {report.triggered_count} triggered tickers.")
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
        layer = Layer(args.layer)
        packet = pipeline.review_single(
            ticker,
            benchmark=args.benchmark.upper(),
            layer=layer,
            run_date=run_date,
            name=getattr(args, "name", None),
        )
        packet_json = json.dumps(packet, indent=2, sort_keys=True)
        print(packet_json)

        # Write to disk alongside daily review packets.
        out_dir = pipeline.storage.data_dir / "reports" / run_date.isoformat() / "review_packets"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{ticker}.json"
        out_path.write_text(packet_json, encoding="utf-8")
        print(f"\n[review-ticker] packet saved to {out_path}", file=sys.stderr)
        return 0

    if args.command == "valuation-order":
        return _handle_valuation_order(pipeline, args, run_date)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
