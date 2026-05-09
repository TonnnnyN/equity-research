from __future__ import annotations

from datetime import date, datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

from market_sentiment.config import EmailDeliveryConfig
from market_sentiment.email_delivery import build_report_message, send_report_email
from market_sentiment.models import DailyRunReport


class EmailDeliveryTests(TestCase):
    def test_send_report_email_uses_smtp_and_formats_message(self) -> None:
        report = DailyRunReport(
            run_date=date(2026, 3, 26),
            generated_at=datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc),
            triggered_count=2,
            scorecards=[],
            source_statuses=[],
        )
        settings = EmailDeliveryConfig(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="sender@example.com",
            password="secret",
            from_address="reports@example.com",
            recipients=["1786146194@qq.com"],
            use_ssl=False,
            use_starttls=True,
            timeout_seconds=12.5,
            subject_prefix="Daily Market Sentiment",
        )

        with patch("market_sentiment.email_delivery.smtplib.SMTP") as smtp_cls:
            smtp_instance = MagicMock()
            smtp_cls.return_value.__enter__.return_value = smtp_instance

            send_report_email(report, "# report body", settings)

        smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=12.5)
        smtp_instance.ehlo.assert_called()
        smtp_instance.starttls.assert_called_once()
        smtp_instance.login.assert_called_once_with("sender@example.com", "secret")
        smtp_instance.send_message.assert_called_once()

        message = smtp_instance.send_message.call_args.args[0]
        self.assertEqual(message["From"], "reports@example.com")
        self.assertEqual(message["To"], "1786146194@qq.com")
        self.assertIn("2026-03-26", message["Subject"])
        self.assertIn("# report body", message.get_content())

    def test_build_report_message_defaults_to_recipient_as_sender_when_unspecified(self) -> None:
        report = DailyRunReport(
            run_date=date(2026, 3, 26),
            generated_at=datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc),
            triggered_count=0,
            scorecards=[],
            source_statuses=[],
        )
        settings = EmailDeliveryConfig(
            smtp_host="smtp.example.com",
            recipients=["1786146194@qq.com"],
        )

        message = build_report_message(report, "report body", settings)

        self.assertEqual(message["From"], "1786146194@qq.com")
        self.assertEqual(message["To"], "1786146194@qq.com")
        self.assertIn("report body", message.get_content())
