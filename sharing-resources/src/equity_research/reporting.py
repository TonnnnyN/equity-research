from __future__ import annotations

from equity_research.models import DailyRunReport


def summarize_report(report: DailyRunReport) -> str:
    lines = [
        f"Run Date: {report.run_date.isoformat()}",
        f"Triggered Tickers: {report.triggered_count}",
        "",
    ]
    for scorecard in report.scorecards:
        lines.append(
            f"{scorecard.security.ticker} | {scorecard.security.layer.value} | "
            f"{scorecard.event_tag.value} | {scorecard.state.value} | {scorecard.total_score}/100"
        )
    return "\n".join(lines)
