"""Tests for the ALLOW_UNSIGNED_TWILIO escape-hatch scoping (audit finding S4)."""
from __future__ import annotations

import pytest

from tools.llm_proxy.twilio_auth import unsigned_twilio_allowed

_PROD_SIGNALS = ("FLY_APP_NAME", "FLY_MACHINE_ID", "ENVIRONMENT", "APP_ENV", "SENTRY_ENV")


@pytest.fixture(autouse=True)
def _clear_prod_signals(monkeypatch):
    """Each test starts from a clean, non-production environment."""
    for var in _PROD_SIGNALS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ALLOW_UNSIGNED_TWILIO", raising=False)


def test_allowed_in_dev_when_flag_set(monkeypatch) -> None:
    """With no production signal and the flag on, the dev hatch works."""
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    assert unsigned_twilio_allowed() is True


def test_refused_by_default(monkeypatch) -> None:
    """Without the flag, signature validation is always required."""
    assert unsigned_twilio_allowed() is False


def test_refused_on_fly_even_with_flag(monkeypatch) -> None:
    """On Fly (FLY_APP_NAME set = production), the hatch is inert even if the
    flag was accidentally left on, forged Twilio webhooks can't bypass HMAC
    signature validation in prod (S4)."""
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    monkeypatch.setenv("FLY_APP_NAME", "ai-receptionist")
    assert unsigned_twilio_allowed() is False


def test_refused_when_environment_is_production(monkeypatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert unsigned_twilio_allowed() is False


def test_refused_when_sentry_env_is_production(monkeypatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    monkeypatch.setenv("SENTRY_ENV", "production")
    assert unsigned_twilio_allowed() is False
