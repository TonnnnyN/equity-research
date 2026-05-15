from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import TestCase
from unittest.mock import patch, MagicMock, Mock

import pandas as pd

from market_sentiment.http import HttpClient
from market_sentiment.models import PriceBar, SourceStatus
from market_sentiment.sources.base import SourcePayload
from market_sentiment.sources.tiger import _to_tiger_symbol, TigerClient
from market_sentiment.storage import Storage


class FakeHttpClient:
    """Minimal HTTP client for testing Tiger client."""
    def get(self, url: str, params=None, headers=None):
        raise NotImplementedError("FakeHttpClient should not be called")


class ToTigerSymbolTests(TestCase):
    """Test cases for the _to_tiger_symbol utility function."""

    def test_to_tiger_symbol_us_stock(self) -> None:
        """Test US stock ticker conversion (should uppercase)."""
        self.assertEqual(_to_tiger_symbol("AAPL"), "AAPL")
        self.assertEqual(_to_tiger_symbol("aapl"), "AAPL")
        self.assertEqual(_to_tiger_symbol("crm"), "CRM")
        self.assertEqual(_to_tiger_symbol("CRM"), "CRM")
        self.assertEqual(_to_tiger_symbol("nvda"), "NVDA")

    def test_to_tiger_symbol_hk_stock_pads_to_five_digits(self) -> None:
        """Test HK stock ticker conversion (pad to 5 digits, preserve .HK suffix)."""
        # 9660 -> 09660.HK (1 digit needs 4 zeros)
        self.assertEqual(_to_tiger_symbol("9660.HK"), "09660.HK")
        # 700 -> 00700.HK (3 digits need 2 zeros)
        self.assertEqual(_to_tiger_symbol("700.HK"), "00700.HK")
        # 3033 -> 03033.HK (4 digits need 1 zero)
        self.assertEqual(_to_tiger_symbol("3033.HK"), "03033.HK")
        # 12345 -> 12345.HK (5 digits, no change)
        self.assertEqual(_to_tiger_symbol("12345.HK"), "12345.HK")
        # 1 -> 00001.HK
        self.assertEqual(_to_tiger_symbol("1.HK"), "00001.HK")


class TigerClientCredentialsTests(TestCase):
    """Test credential validation and failure modes."""

    def test_missing_credentials_returns_partial_failure(self) -> None:
        """Test that missing TIGER_CONFIG_PATH returns partial failure."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            http_client = FakeHttpClient()

            # Clear environment variables
            with patch.dict(os.environ, {}, clear=True):
                client = TigerClient(http_client, storage)
                payload = client.fetch_daily_prices("AAPL", date(2026, 5, 15))

                self.assertFalse(payload.status.success)
                self.assertTrue(payload.status.partial)
                self.assertEqual(payload.status.source, "tiger")
                self.assertIn("TIGER_CONFIG_PATH", payload.status.message)
                self.assertEqual(payload.data, [])


    def test_properties_file_missing_returns_partial_failure(self) -> None:
        """Test that a non-existent config path returns partial failure."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            http_client = FakeHttpClient()
            nonexistent_path = "/nonexistent/path/to/config"

            with patch.dict(
                os.environ,
                {"TIGER_CONFIG_PATH": nonexistent_path},
                clear=True
            ):
                client = TigerClient(http_client, storage)
                payload = client.fetch_daily_prices("AAPL", date(2026, 5, 15))

                self.assertFalse(payload.status.success)
                self.assertTrue(payload.status.partial)
                self.assertIn("not found", payload.status.message.lower())
                self.assertEqual(payload.data, [])


class TigerClientSDKImportTests(TestCase):
    """Test SDK availability checks."""

    def test_tigeropen_not_installed_returns_partial_failure(self) -> None:
        """Test that when tigeropen SDK is not installed, fetch returns partial failure."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            http_client = FakeHttpClient()

            # Create a temporary directory with an empty properties file
            with tempfile.TemporaryDirectory() as props_dir:
                props_file = Path(props_dir) / "tiger_openapi_config.properties"
                props_file.touch()

                # Mock the tigeropen import to fail
                mock_modules = {
                    "tigeropen": None,
                    "tigeropen.common": None,
                    "tigeropen.common.consts": None,
                    "tigeropen.quote": None,
                    "tigeropen.quote.quote_client": None,
                }

                with patch.dict(
                    os.environ,
                    {"TIGER_CONFIG_PATH": props_dir},
                    clear=True
                ):
                    with patch.dict(sys.modules, mock_modules):
                        client = TigerClient(http_client, storage)
                        payload = client.fetch_daily_prices("AAPL", date(2026, 5, 15))

                self.assertFalse(payload.status.success)
                self.assertTrue(payload.status.partial)
                self.assertIn("tigeropen", payload.status.message.lower())
                self.assertEqual(payload.data, [])


class TigerClientSuccessTests(TestCase):
    """Test successful data parsing."""

    def test_successful_fetch_parses_dataframe_to_pricebars(self) -> None:
        """Test that successful SDK call returns parsed PriceBar objects."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            http_client = FakeHttpClient()

            # Create a temporary directory with an empty properties file
            with tempfile.TemporaryDirectory() as props_dir:
                props_file = Path(props_dir) / "tiger_openapi_config.properties"
                props_file.touch()

                # Create fake DataFrame with 3 rows of price data
                # Tiger API returns time in milliseconds
                df_data = {
                    'time': [
                        1715596800000,  # 2024-05-13 00:00:00 UTC
                        1715683200000,  # 2024-05-14 00:00:00 UTC
                        1715769600000,  # 2024-05-15 00:00:00 UTC
                    ],
                    'open': [100.0, 101.5, 102.3],
                    'high': [101.0, 102.5, 103.2],
                    'low': [99.5, 100.8, 101.5],
                    'close': [100.5, 102.0, 102.8],
                    'volume': [1000000.0, 1100000.0, 950000.0],
                }
                fake_df = pd.DataFrame(df_data)

                # Create mock tigeropen modules
                mock_config_class = Mock()
                mock_quote_client_class = Mock()
                mock_quote_instance = Mock()
                mock_quote_client_class.return_value = mock_quote_instance
                mock_quote_instance.get_bars.return_value = fake_df

                # Create module structure
                tigeropen_module = ModuleType("tigeropen")
                tigeropen_tiger_open_config_module = ModuleType("tigeropen.tiger_open_config")
                tigeropen_quote_module = ModuleType("tigeropen.quote")
                tigeropen_quote_client_module = ModuleType("tigeropen.quote.quote_client")

                tigeropen_tiger_open_config_module.TigerOpenClientConfig = mock_config_class
                tigeropen_quote_client_module.QuoteClient = mock_quote_client_class

                mock_modules = {
                    "tigeropen": tigeropen_module,
                    "tigeropen.tiger_open_config": tigeropen_tiger_open_config_module,
                    "tigeropen.quote": tigeropen_quote_module,
                    "tigeropen.quote.quote_client": tigeropen_quote_client_module,
                }

                with patch.dict(
                    os.environ,
                    {"TIGER_CONFIG_PATH": props_dir},
                    clear=True
                ):
                    with patch.dict(sys.modules, mock_modules):
                        client = TigerClient(http_client, storage)
                        payload = client.fetch_daily_prices("AAPL", date(2026, 5, 15))

                # Verify success
                self.assertTrue(payload.status.success)
                self.assertFalse(payload.status.partial)
                self.assertEqual(payload.status.source, "tiger")
                self.assertEqual(payload.status.message, "ok")

                # Verify price data
                self.assertEqual(len(payload.data), 3)

                # All should have source == "tiger"
                for price_bar in payload.data:
                    self.assertEqual(price_bar.source, "tiger")
                    self.assertEqual(price_bar.ticker, "AAPL")

                # Verify prices are in ascending date order
                dates = [pb.trading_date for pb in payload.data]
                self.assertEqual(dates, sorted(dates))

                # Verify first price bar values
                first = payload.data[0]
                self.assertEqual(first.open, 100.0)
                self.assertEqual(first.high, 101.0)
                self.assertEqual(first.low, 99.5)
                self.assertEqual(first.close, 100.5)
                self.assertEqual(first.volume, 1000000.0)

                # Verify TigerOpenClientConfig was called with props_dir (resolved path)
                called_args = mock_config_class.call_args
                self.assertIsNotNone(called_args)
                # props_path should be the string version of the resolved directory
                called_props_path = called_args.kwargs.get("props_path")
                self.assertEqual(str(Path(props_dir).resolve()), str(Path(called_props_path).resolve()))

    def test_empty_dataframe_returns_partial(self) -> None:
        """Test that an empty DataFrame response returns partial failure."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            http_client = FakeHttpClient()

            # Create a temporary directory with an empty properties file
            with tempfile.TemporaryDirectory() as props_dir:
                props_file = Path(props_dir) / "tiger_openapi_config.properties"
                props_file.touch()

                # Create empty DataFrame
                fake_df = pd.DataFrame()

                # Create mock tigeropen modules
                mock_config_class = Mock()
                mock_quote_client_class = Mock()
                mock_quote_instance = Mock()
                mock_quote_client_class.return_value = mock_quote_instance
                mock_quote_instance.get_bars.return_value = fake_df

                # Create module structure
                tigeropen_module = ModuleType("tigeropen")
                tigeropen_tiger_open_config_module = ModuleType("tigeropen.tiger_open_config")
                tigeropen_quote_module = ModuleType("tigeropen.quote")
                tigeropen_quote_client_module = ModuleType("tigeropen.quote.quote_client")

                tigeropen_tiger_open_config_module.TigerOpenClientConfig = mock_config_class
                tigeropen_quote_client_module.QuoteClient = mock_quote_client_class

                mock_modules = {
                    "tigeropen": tigeropen_module,
                    "tigeropen.tiger_open_config": tigeropen_tiger_open_config_module,
                    "tigeropen.quote": tigeropen_quote_module,
                    "tigeropen.quote.quote_client": tigeropen_quote_client_module,
                }

                with patch.dict(
                    os.environ,
                    {"TIGER_CONFIG_PATH": props_dir},
                    clear=True
                ):
                    with patch.dict(sys.modules, mock_modules):
                        client = TigerClient(http_client, storage)
                        payload = client.fetch_daily_prices("AAPL", date(2026, 5, 15))

                # Verify partial failure
                self.assertFalse(payload.status.success)
                self.assertTrue(payload.status.partial)
                self.assertEqual(payload.status.source, "tiger")
                self.assertIn("empty", payload.status.message.lower())
                self.assertEqual(payload.data, [])

    def test_sdk_call_raises_exception_returns_partial(self) -> None:
        """Test that SDK exceptions during get_bars are caught and return partial failure."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            http_client = FakeHttpClient()

            # Create a temporary directory with an empty properties file
            with tempfile.TemporaryDirectory() as props_dir:
                props_file = Path(props_dir) / "tiger_openapi_config.properties"
                props_file.touch()

                # Create mock tigeropen modules
                mock_config_class = Mock()
                mock_quote_client_class = Mock()
                mock_quote_instance = Mock()
                mock_quote_client_class.return_value = mock_quote_instance
                mock_quote_instance.get_bars.side_effect = RuntimeError("auth failed")

                # Create module structure
                tigeropen_module = ModuleType("tigeropen")
                tigeropen_tiger_open_config_module = ModuleType("tigeropen.tiger_open_config")
                tigeropen_quote_module = ModuleType("tigeropen.quote")
                tigeropen_quote_client_module = ModuleType("tigeropen.quote.quote_client")

                tigeropen_tiger_open_config_module.TigerOpenClientConfig = mock_config_class
                tigeropen_quote_client_module.QuoteClient = mock_quote_client_class

                mock_modules = {
                    "tigeropen": tigeropen_module,
                    "tigeropen.tiger_open_config": tigeropen_tiger_open_config_module,
                    "tigeropen.quote": tigeropen_quote_module,
                    "tigeropen.quote.quote_client": tigeropen_quote_client_module,
                }

                with patch.dict(
                    os.environ,
                    {"TIGER_CONFIG_PATH": props_dir},
                    clear=True
                ):
                    with patch.dict(sys.modules, mock_modules):
                        client = TigerClient(http_client, storage)
                        payload = client.fetch_daily_prices("AAPL", date(2026, 5, 15))

                # Verify partial failure
                self.assertFalse(payload.status.success)
                self.assertTrue(payload.status.partial)
                self.assertEqual(payload.status.source, "tiger")
                self.assertIn("Failed to fetch bars", payload.status.message)
                self.assertEqual(payload.data, [])

    def test_hk_stock_with_successful_fetch(self) -> None:
        """Test fetching HK stock (with padded symbol conversion)."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            http_client = FakeHttpClient()

            # Create a temporary directory with an empty properties file
            with tempfile.TemporaryDirectory() as props_dir:
                props_file = Path(props_dir) / "tiger_openapi_config.properties"
                props_file.touch()

                # Create fake DataFrame with 1 row of price data
                df_data = {
                    'time': [1715596800000],  # 2024-05-13 00:00:00 UTC
                    'open': [500.0],
                    'high': [510.0],
                    'low': [495.0],
                    'close': [505.0],
                    'volume': [2000000.0],
                }
                fake_df = pd.DataFrame(df_data)

                # Create mock tigeropen modules
                mock_config_class = Mock()
                mock_quote_client_class = Mock()
                mock_quote_instance = Mock()
                mock_quote_client_class.return_value = mock_quote_instance
                mock_quote_instance.get_bars.return_value = fake_df

                # Create module structure
                tigeropen_module = ModuleType("tigeropen")
                tigeropen_tiger_open_config_module = ModuleType("tigeropen.tiger_open_config")
                tigeropen_quote_module = ModuleType("tigeropen.quote")
                tigeropen_quote_client_module = ModuleType("tigeropen.quote.quote_client")

                tigeropen_tiger_open_config_module.TigerOpenClientConfig = mock_config_class
                tigeropen_quote_client_module.QuoteClient = mock_quote_client_class

                mock_modules = {
                    "tigeropen": tigeropen_module,
                    "tigeropen.tiger_open_config": tigeropen_tiger_open_config_module,
                    "tigeropen.quote": tigeropen_quote_module,
                    "tigeropen.quote.quote_client": tigeropen_quote_client_module,
                }

                with patch.dict(
                    os.environ,
                    {"TIGER_CONFIG_PATH": props_dir},
                    clear=True
                ):
                    with patch.dict(sys.modules, mock_modules):
                        client = TigerClient(http_client, storage)
                        payload = client.fetch_daily_prices("9660.HK", date(2026, 5, 15))

                # Verify success
                self.assertTrue(payload.status.success)
                self.assertEqual(len(payload.data), 1)

                # Original ticker should be preserved in PriceBar
                self.assertEqual(payload.data[0].ticker, "9660.HK")
                self.assertEqual(payload.data[0].source, "tiger")

                # Verify that get_bars was called with the padded symbol
                called_args = mock_quote_instance.get_bars.call_args
                self.assertIsNotNone(called_args)
                self.assertIn("09660.HK", str(called_args))

    def test_quote_client_constructed_with_is_grab_permission_false(self) -> None:
        """QuoteClient must be constructed with is_grab_permission=False to skip the
        startup grab_quote_permission call that requires a real-time user_token."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "db.sqlite3", Path(tmp))
            storage.init_db()

            http_client = FakeHttpClient()

            with tempfile.TemporaryDirectory() as props_dir:
                props_file = Path(props_dir) / "tiger_openapi_config.properties"
                props_file.touch()

                fake_df = pd.DataFrame({
                    'time': [1715596800000],
                    'open': [100.0],
                    'high': [101.0],
                    'low': [99.5],
                    'close': [100.5],
                    'volume': [1000000.0],
                })

                mock_config_class = Mock()
                mock_quote_client_class = Mock()
                mock_quote_instance = Mock()
                mock_quote_client_class.return_value = mock_quote_instance
                mock_quote_instance.get_bars.return_value = fake_df

                tigeropen_module = ModuleType("tigeropen")
                tigeropen_tiger_open_config_module = ModuleType("tigeropen.tiger_open_config")
                tigeropen_quote_module = ModuleType("tigeropen.quote")
                tigeropen_quote_client_module = ModuleType("tigeropen.quote.quote_client")

                tigeropen_tiger_open_config_module.TigerOpenClientConfig = mock_config_class
                tigeropen_quote_client_module.QuoteClient = mock_quote_client_class

                mock_modules = {
                    "tigeropen": tigeropen_module,
                    "tigeropen.tiger_open_config": tigeropen_tiger_open_config_module,
                    "tigeropen.quote": tigeropen_quote_module,
                    "tigeropen.quote.quote_client": tigeropen_quote_client_module,
                }

                with patch.dict(os.environ, {"TIGER_CONFIG_PATH": props_dir}, clear=True):
                    with patch.dict(sys.modules, mock_modules):
                        client = TigerClient(http_client, storage)
                        payload = client.fetch_daily_prices("AAPL", date(2026, 5, 15))

                # Confirm the call succeeded
                self.assertTrue(payload.status.success)

                # The critical assertion: QuoteClient must have been called with
                # is_grab_permission=False (kwarg form, matching SDK signature).
                mock_quote_client_class.assert_called_once()
                call_kwargs = mock_quote_client_class.call_args.kwargs
                self.assertIn("is_grab_permission", call_kwargs)
                self.assertFalse(call_kwargs["is_grab_permission"])
