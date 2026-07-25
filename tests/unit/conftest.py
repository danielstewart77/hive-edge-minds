"""Shared test fixtures for unit tests.

Provides mock modules for third-party dependencies that are not installed
in the test environment (telegram).
"""

import sys
import types
from unittest.mock import MagicMock

import pytest


def _create_mock_module(name: str, submodules: dict | None = None) -> MagicMock:
    """Create a mock module with optional submodules."""
    mod = MagicMock(spec=types.ModuleType)
    mod.__name__ = name
    if submodules:
        for sub_name, sub_mock in submodules.items():
            setattr(mod, sub_name, sub_mock)
    return mod


@pytest.fixture(autouse=True)
def _mock_third_party_modules(monkeypatch):
    """Mock the telegram module for all unit tests.

    This fixture ensures that tests can import bots/telegram_bot.py without
    having python-telegram-bot installed in the test environment.
    """
    # Remove any previously cached imports of the client modules so they
    # get re-imported with our mocked deps each time.
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("bots."):
            del sys.modules[mod_name]

    # --- Telegram mocks ---
    telegram_mock = _create_mock_module("telegram")
    telegram_mock.Update = MagicMock()

    telegram_ext_mock = _create_mock_module("telegram.ext")
    telegram_ext_mock.ApplicationBuilder = MagicMock()
    telegram_ext_mock.CallbackQueryHandler = MagicMock()
    telegram_ext_mock.CommandHandler = MagicMock()
    telegram_ext_mock.ContextTypes = MagicMock()
    telegram_ext_mock.MessageHandler = MagicMock()
    telegram_ext_mock.filters = MagicMock()

    telegram_error_mock = _create_mock_module("telegram.error")
    telegram_error_mock.NetworkError = type("NetworkError", (Exception,), {})
    telegram_error_mock.TimedOut = type("TimedOut", (Exception,), {})

    monkeypatch.setitem(sys.modules, "telegram", telegram_mock)
    monkeypatch.setitem(sys.modules, "telegram.ext", telegram_ext_mock)
    monkeypatch.setitem(sys.modules, "telegram.error", telegram_error_mock)

    # --- Keyring mock (for _get_bot_token) ---
    keyring_mock = _create_mock_module("keyring")
    keyring_mock.get_password = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "keyring", keyring_mock)

    # --- requests mock (used by stateless tool modules) ---
    if "requests" not in sys.modules:
        requests_mock = _create_mock_module("requests")
        requests_mock.post = MagicMock()
        requests_mock.get = MagicMock()
        requests_mock.put = MagicMock()
        requests_mock.delete = MagicMock()
        requests_mock.RequestException = Exception
        requests_mock.HTTPError = Exception
        requests_mock.ConnectionError = Exception
        monkeypatch.setitem(sys.modules, "requests", requests_mock)

    # --- Anthropic SDK mock (for minds/nagatha/) ---
    if "anthropic" not in sys.modules:
        anthropic_mock = _create_mock_module("anthropic")
        anthropic_mock.AsyncAnthropic = MagicMock()
        monkeypatch.setitem(sys.modules, "anthropic", anthropic_mock)
