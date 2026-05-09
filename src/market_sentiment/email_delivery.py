from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import format_datetime

from market_sentiment.config import EmailDeliveryConfig
from market_sentiment.models import DailyRunReport


def send_report_email(report: DailyRunReport, report_markdown: str, settings: EmailDeliveryConfig) -> None:
    if not settings.smtp_host:
        raise ValueError("SMTP host is not configured.")
    if not settings.recipients:
        raise ValueError("At least one recipient is required.")
    if settings.use_ssl and settings.use_starttls:
        raise ValueError("Configure only one of use_ssl or use_starttls.")
    if bool(settings.username) != bool(settings.password):
        raise ValueError("SMTP username and password must either both be set or both be omitted.")

    message = build_report_message(report, report_markdown, settings)
    ssl_context = ssl.create_default_context()

    if settings.use_ssl:
        with smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.timeout_seconds,
            context=ssl_context,
        ) as smtp:
            _deliver(smtp, message, settings)
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.timeout_seconds) as smtp:
        smtp.ehlo()
        if settings.use_starttls:
            smtp.starttls(context=ssl_context)
            smtp.ehlo()
        _deliver(smtp, message, settings)


def build_report_message(
    report: DailyRunReport,
    report_markdown: str,
    settings: EmailDeliveryConfig,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = (
        f"{settings.subject_prefix} - {report.run_date.isoformat()} ({report.triggered_count} triggered)"
    )
    message["From"] = settings.from_address or settings.username or settings.recipients[0]
    message["To"] = ", ".join(settings.recipients)
    message["Date"] = format_datetime(report.generated_at)
    message.set_content(report_markdown)
    return message


def _deliver(smtp: smtplib.SMTP, message: EmailMessage, settings: EmailDeliveryConfig) -> None:
    if settings.username and settings.password:
        smtp.login(settings.username, settings.password)
    smtp.send_message(message)
