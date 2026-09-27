"""Tests for the post-call SMS notifier.

Stubs the Twilio REST client (`messages.create`) so the customer-confirmation
and owner-emergency paths run end-to-end without hitting Twilio. Twilio's
client is synchronous; the notifier wraps it in `asyncio.to_thread`, which
the fake client exercises faithfully.
"""
from __future__ import annotations

from typing import Any

import pytest
from twilio.base.exceptions import TwilioRestException

from tools.llm_proxy.sms_notifier import SmsNotifier, notifier_from_env


class FakeMessages:
    def __init__(self, *, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._exc = exc

    def create(self, **kwargs: Any) -> Any:
        if self._exc is not None:
            raise self._exc
        self.calls.append(kwargs)
        return object()


class FakeTwilioClient:
    def __init__(self, *, exc: Exception | None = None) -> None:
        self.messages = FakeMessages(exc=exc)


def _make_notifier(
    *,
    exc: Exception | None = None,
    owner: str = "+15550009999",
    business_name: str = "Clear Slope Pest Control",
    business_short_name: str = "Clear Slope",
) -> tuple[SmsNotifier, FakeTwilioClient]:
    fake = FakeTwilioClient(exc=exc)
    notifier = SmsNotifier(
        client=fake,
        from_number="+15550001111",
        owner_number=owner,
        business_name=business_name,
        business_short_name=business_short_name,
    )
    return notifier, fake


@pytest.mark.asyncio
async def test_customer_confirmation_uses_first_name_and_routine_phrasing() -> None:
    notifier, fake = _make_notifier()
    ok = await notifier.send_customer_confirmation(
        to="+16045550123", caller_name="Alex Doe", urgency="routine",
    )
    assert ok is True
    assert len(fake.messages.calls) == 1
    sent = fake.messages.calls[0]
    assert sent["to"] == "+16045550123"
    assert sent["from_"] == "+15550001111"
    assert "Alex" in sent["body"]
    assert "Doe" not in sent["body"]
    assert "next business day" in sent["body"].lower()


@pytest.mark.asyncio
async def test_customer_confirmation_emergency_phrasing() -> None:
    notifier, fake = _make_notifier()
    ok = await notifier.send_customer_confirmation(
        to="+16045550123", caller_name="Alex", urgency="emergency",
    )
    assert ok is True
    assert "shortly" in fake.messages.calls[0]["body"].lower()


@pytest.mark.asyncio
async def test_customer_confirmation_soon_phrasing() -> None:
    notifier, fake = _make_notifier()
    await notifier.send_customer_confirmation(
        to="+16045550123", caller_name="Alex", urgency="soon",
    )
    assert "today" in fake.messages.calls[0]["body"].lower()


@pytest.mark.asyncio
async def test_customer_confirmation_skips_when_no_to_number() -> None:
    notifier, fake = _make_notifier()
    ok = await notifier.send_customer_confirmation(
        to="", caller_name="Alex", urgency="routine",
    )
    assert ok is False
    assert fake.messages.calls == []


@pytest.mark.asyncio
async def test_customer_confirmation_skips_anonymous_callers() -> None:
    notifier, fake = _make_notifier()
    for label in ("anonymous", "+anonymous", "unavailable", "Restricted"):
        ok = await notifier.send_customer_confirmation(
            to=label, caller_name="Alex", urgency="routine",
        )
        assert ok is False, f"should reject {label!r}"
    assert fake.messages.calls == []


@pytest.mark.asyncio
async def test_customer_confirmation_returns_false_on_twilio_failure() -> None:
    notifier, fake = _make_notifier(
        exc=TwilioRestException(status=400, uri="/", msg="bad number"),
    )
    ok = await notifier.send_customer_confirmation(
        to="+16045550123", caller_name="Alex", urgency="routine",
    )
    assert ok is False
    assert fake.messages.calls == []  # exception path didn't append


@pytest.mark.asyncio
async def test_owner_emergency_alert_includes_caller_details() -> None:
    notifier, fake = _make_notifier(owner="+15559998888")
    ok = await notifier.send_owner_emergency_alert(
        caller_name="Alex Doe",
        address="100 Main St",
        pest="norway rat",
        callback="+16045550123",
    )
    assert ok is True
    assert len(fake.messages.calls) == 1
    sent = fake.messages.calls[0]
    assert sent["to"] == "+15559998888"
    assert sent["from_"] == "+15550001111"
    body = sent["body"]
    assert "Alex Doe" in body
    assert "100 Main St" in body
    assert "norway rat" in body
    assert "+16045550123" in body
    assert "EMERGENCY" in body.upper()


@pytest.mark.asyncio
async def test_owner_alert_skipped_when_no_owner_number_configured() -> None:
    notifier, fake = _make_notifier(owner="")
    ok = await notifier.send_owner_emergency_alert(
        caller_name="Alex", address="100 Main St", pest="rat", callback="+1",
    )
    assert ok is False
    assert fake.messages.calls == []


@pytest.mark.asyncio
async def test_customer_message_uses_business_name_from_config() -> None:
    """A second tenant should see their own brand name in the SMS body."""
    notifier, fake = _make_notifier(
        business_name="Bob's Plumbing",
        business_short_name="Bob's",
    )
    await notifier.send_customer_confirmation(
        to="+16045550123", caller_name="Alex", urgency="routine",
    )
    body = fake.messages.calls[0]["body"]
    assert "Bob's Plumbing" in body
    assert ", Bob's." in body
    assert "Clear Slope" not in body


@pytest.mark.asyncio
async def test_owner_alert_uses_business_short_name_from_config() -> None:
    notifier, fake = _make_notifier(
        owner="+15559998888",
        business_short_name="Bob's",
    )
    await notifier.send_owner_emergency_alert(
        caller_name="Alex", address="100 Main St", pest="rat", callback="+1",
    )
    body = fake.messages.calls[0]["body"]
    assert body.startswith("EMERGENCY intake, Bob's")
    assert "Clear Slope" not in body


@pytest.mark.asyncio
async def test_owner_alert_returns_false_on_twilio_failure() -> None:
    notifier, _ = _make_notifier(
        exc=TwilioRestException(status=500, uri="/", msg="twilio down"),
    )
    ok = await notifier.send_owner_emergency_alert(
        caller_name="Alex", address="100 Main St", pest="rat", callback="+1",
    )
    assert ok is False


_FROM_ENV_KWARGS: dict[str, str] = {
    "prefix": "ACME",
    "business_name": "Clear Slope Pest Control",
    "business_short_name": "Clear Slope",
}


def test_notifier_from_env_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "ACME_TWILIO_SMS_FROM",
        "ACME_OWNER_NOTIFY_NUMBER",
    ):
        monkeypatch.delenv(var, raising=False)
    assert notifier_from_env(**_FROM_ENV_KWARGS) is None


def test_notifier_from_env_returns_none_when_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.delenv("ACME_TWILIO_SMS_FROM", raising=False)
    assert notifier_from_env(**_FROM_ENV_KWARGS) is None


def test_notifier_from_env_constructs_when_all_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("ACME_TWILIO_SMS_FROM", "+15550001111")
    monkeypatch.setenv("ACME_OWNER_NOTIFY_NUMBER", "+15559998888")
    notifier = notifier_from_env(**_FROM_ENV_KWARGS)
    assert notifier is not None
    assert notifier.from_number == "+15550001111"
    assert notifier.owner_number == "+15559998888"


def test_notifier_from_env_owner_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("ACME_TWILIO_SMS_FROM", "+15550001111")
    monkeypatch.delenv("ACME_OWNER_NOTIFY_NUMBER", raising=False)
    notifier = notifier_from_env(**_FROM_ENV_KWARGS)
    assert notifier is not None
    assert notifier.owner_number == ""


@pytest.mark.asyncio
async def test_voicemail_alert_includes_caller_duration_transcript_and_url() -> None:
    notifier, fake = _make_notifier(business_short_name="All Green")
    ok = await notifier.send_voicemail_alert(
        caller_number="+16045550123",
        duration_seconds=42,
        transcript="Hi this is Bob, my house has rats.",
        playback_url="https://example.fly.dev/vm/REabc?exp=1&sig=x",
    )
    assert ok is True
    sent = fake.messages.calls[0]
    assert sent["to"] == "+15550009999"  # owner
    body = sent["body"]
    assert "All Green" in body
    assert "***0123" in body
    assert "0:42" in body
    assert "Hi this is Bob" in body
    assert "https://example.fly.dev/vm/REabc?exp=1&sig=x" in body


@pytest.mark.asyncio
async def test_voicemail_alert_anonymous_caller_renders_anonymous() -> None:
    notifier, fake = _make_notifier()
    ok = await notifier.send_voicemail_alert(
        caller_number="",
        duration_seconds=10,
        transcript="hello",
        playback_url="https://x/vm/RE1?exp=1&sig=s",
    )
    assert ok is True
    assert "<anonymous>" in fake.messages.calls[0]["body"]


@pytest.mark.asyncio
async def test_voicemail_alert_empty_transcript_uses_placeholder() -> None:
    notifier, fake = _make_notifier()
    ok = await notifier.send_voicemail_alert(
        caller_number="+16045550123",
        duration_seconds=12,
        transcript="",
        playback_url="https://x/vm/RE1?exp=1&sig=s",
    )
    assert ok is True
    assert "(no transcript available)" in fake.messages.calls[0]["body"]


@pytest.mark.asyncio
async def test_voicemail_alert_skipped_if_no_owner_number() -> None:
    notifier, fake = _make_notifier(owner="")
    ok = await notifier.send_voicemail_alert(
        caller_number="+16045550123",
        duration_seconds=12,
        transcript="hi",
        playback_url="https://x/vm/RE1?exp=1&sig=s",
    )
    assert ok is False
    assert fake.messages.calls == []


@pytest.mark.asyncio
async def test_voicemail_alert_truncates_long_transcript() -> None:
    notifier, fake = _make_notifier()
    transcript = "x" * 500
    ok = await notifier.send_voicemail_alert(
        caller_number="+16045550123",
        duration_seconds=120,
        transcript=transcript,
        playback_url="https://x/vm/RE1?exp=1&sig=s",
    )
    assert ok is True
    body = fake.messages.calls[0]["body"]
    assert "xxx…" in body
    assert "x" * 301 not in body
