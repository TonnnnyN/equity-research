from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from market_sentiment.config import DEFAULT_REPORT_RECIPIENT, load_config


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "watchlist.toml"


class ConfigTests(TestCase):
    def test_load_config_parses_email_delivery_settings_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overrides = {
                "MARKET_SENTIMENT_DATA_DIR": tmp,
                "MARKET_SENTIMENT_DB_PATH": str(Path(tmp) / "market_sentiment.db"),
                "MARKET_SENTIMENT_SMTP_HOST": "smtp.example.com",
                "MARKET_SENTIMENT_SMTP_PORT": "465",
                "MARKET_SENTIMENT_SMTP_USERNAME": "sender@example.com",
                "MARKET_SENTIMENT_SMTP_PASSWORD": "secret",
                "MARKET_SENTIMENT_EMAIL_FROM": "reports@example.com",
                "MARKET_SENTIMENT_REPORT_EMAIL_TO": "1786146194@qq.com,alice@example.com",
                "MARKET_SENTIMENT_SMTP_USE_SSL": "true",
                "MARKET_SENTIMENT_SMTP_USE_STARTTLS": "false",
                "MARKET_SENTIMENT_SMTP_TIMEOUT_SECONDS": "12.5",
                "MARKET_SENTIMENT_EMAIL_SUBJECT_PREFIX": "Daily Market Sentiment",
            }
            with patch.dict(os.environ, overrides, clear=False):
                config = load_config(str(CONFIG_PATH))

        self.assertTrue(config.report_email.is_configured())
        self.assertEqual(config.report_email.smtp_host, "smtp.example.com")
        self.assertEqual(config.report_email.smtp_port, 465)
        self.assertEqual(config.report_email.username, "sender@example.com")
        self.assertEqual(config.report_email.password, "secret")
        self.assertEqual(config.report_email.from_address, "reports@example.com")
        self.assertEqual(config.report_email.recipients, ["1786146194@qq.com", "alice@example.com"])
        self.assertTrue(config.report_email.use_ssl)
        self.assertFalse(config.report_email.use_starttls)
        self.assertEqual(config.report_email.timeout_seconds, 12.5)
        self.assertEqual(config.report_email.subject_prefix, "Daily Market Sentiment")

    def test_load_config_defaults_to_report_recipient_when_unspecified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overrides = {
                "MARKET_SENTIMENT_DATA_DIR": tmp,
                "MARKET_SENTIMENT_DB_PATH": str(Path(tmp) / "market_sentiment.db"),
            }
            with patch.dict(os.environ, overrides, clear=False):
                config = load_config(str(CONFIG_PATH))

        self.assertEqual(config.report_email.recipients, [DEFAULT_REPORT_RECIPIENT])

    def test_load_config_applies_default_retention_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overrides = {
                "MARKET_SENTIMENT_DATA_DIR": tmp,
                "MARKET_SENTIMENT_DB_PATH": str(Path(tmp) / "market_sentiment.db"),
            }
            with patch.dict(os.environ, overrides, clear=False):
                config = load_config(str(CONFIG_PATH))

        self.assertEqual(config.retention.report_days, 90)
        self.assertEqual(config.retention.raw_payload_days, 30)
        self.assertEqual(config.retention.daily_price_days, 365)
        self.assertEqual(config.retention.fundamental_days, 730)
        self.assertEqual(config.retention.social_post_days, 90)
        self.assertEqual(config.retention.social_snapshot_days, 180)

    def test_load_config_parses_retention_overrides_from_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "watchlist.toml"
            config_path.write_text(
                """
[project]
timezone = "Asia/Hong_Kong"
db_path = "data/state/market_sentiment.sqlite3"

[triggers.compute]
drawdown_10d = -0.10
drawdown_20d = -0.20
relative_20d = -0.05

[retention]
report_days = 14
raw_payload_days = 7
daily_price_days = 120
official_event_days = 180
fundamental_days = 365
macro_days = 60
run_metadata_days = 21
social_post_days = 45
social_snapshot_days = 120

[social]
enabled = true
providers = ["reddit", "forum", "x"]

[social.forum]
enabled = true
base_urls = ["https://forum.example.com"]
max_posts_per_forum = 25

[social.x]
enabled = true
provider = "twscrape"
max_posts = 15
""".strip(),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=False):
                config = load_config(str(config_path))

        self.assertEqual(config.retention.report_days, 14)
        self.assertEqual(config.retention.raw_payload_days, 7)
        self.assertEqual(config.retention.daily_price_days, 120)
        self.assertEqual(config.retention.official_event_days, 180)
        self.assertEqual(config.retention.fundamental_days, 365)
        self.assertEqual(config.retention.macro_days, 60)
        self.assertEqual(config.retention.run_metadata_days, 21)
        self.assertEqual(config.retention.social_post_days, 45)
        self.assertEqual(config.retention.social_snapshot_days, 120)
        self.assertTrue(config.social.enabled)
        self.assertEqual(config.social.providers, ["reddit", "forum", "x"])
        self.assertTrue(config.social.forum.enabled)
        self.assertEqual(config.social.forum.base_urls, ["https://forum.example.com"])
        self.assertTrue(config.social.x.enabled)
        self.assertEqual(config.social.x.provider, "twscrape")

    def test_load_config_parses_x_social_overrides_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overrides = {
                "MARKET_SENTIMENT_DATA_DIR": tmp,
                "MARKET_SENTIMENT_DB_PATH": str(Path(tmp) / "market_sentiment.db"),
                "SOCIAL_ENABLED": "true",
                "SOCIAL_PROVIDER_TIMEOUT_SECONDS": "7.5",
                "X_ENABLED": "true",
                "X_PROVIDER": "twscrape,twikit",
                "X_SEARCH_PRODUCT": "top",
                "X_PROXY_URL": "http://proxy.local:8080",
                "X_COOKIES_PATH": str(Path(tmp) / "x_cookies.json"),
                "X_ACCOUNTS_FILE": str(Path(tmp) / "accounts.txt"),
            }
            with patch.dict(os.environ, overrides, clear=False):
                config = load_config(str(CONFIG_PATH))

        self.assertTrue(config.social.enabled)
        self.assertEqual(config.social.provider_timeout_seconds, 7.5)
        self.assertTrue(config.social.x.enabled)
        self.assertEqual(config.social.x.provider, "twscrape,twikit")
        self.assertEqual(config.social.x.search_product, "Top")
        self.assertEqual(config.social.x.proxy_url, "http://proxy.local:8080")
        self.assertEqual(config.social.x.accounts_file, str(Path(tmp) / "accounts.txt"))

    def test_load_config_parses_options_overrides_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overrides = {
                "MARKET_SENTIMENT_DATA_DIR": tmp,
                "MARKET_SENTIMENT_DB_PATH": str(Path(tmp) / "market_sentiment.db"),
                "OPTIONS_ENABLED": "true",
                "OPTIONS_PROVIDER": "alpha_vantage",
                "OPTIONS_REQUIRE_GREEKS": "true",
                "OPTIONS_MAX_CONTRACTS": "80",
            }
            with patch.dict(os.environ, overrides, clear=False):
                config = load_config(str(CONFIG_PATH))

        self.assertTrue(config.options.enabled)
        self.assertEqual(config.options.provider, "alpha_vantage")
        self.assertTrue(config.options.require_greeks)
        self.assertEqual(config.options.max_contracts, 80)
