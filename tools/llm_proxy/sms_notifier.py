"""Post-call SMS notifications via Twilio.

Two channels:

  • Customer confirmation, sent to the caller after `log_request` succeeds.
    Closes the loop ("we got your request, we'll follow up") so the caller
    isn't left wondering whether a human heard them.

  • Owner emergency alert, sent to the proprietor when urgency=='emergency'.
    Pushes hot leads (active rodents, wasps inside, etc.) past the regular
    Sheets-review queue and onto the owner's phone immediately.

Twilio's Python REST client is synchronous, so each `messages.create` call is
dispatched onto a worker thread via `asyncio.to_thread`. Failures are caught
and logged, SMS sends never block or break the call path.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Protocol

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

logger = logging.getLogger("sms_notifier")

# Caller-ID strings Twilio surfaces for blocked / unavailable callers. We
# can't text those numbers back, so skip the customer confirmation in those
# cases (the row still lands in the Sheet).
_ANONYMOUS_FROM_VALUES = {"anonymous", "+anonymous", "unavailable", "restricted", "private"}


class _MessagesClient(Protocol):
    def create(self, *, to: str, from_: str, body: str) -> Any: ...


class _SupportsMessages(Protocol):
    messages: _MessagesClient


class SmsNotifier:
    """Wraps a Twilio REST client to send post-call SMS.

    The `client` parameter is typed structurally so tests can pass a fake
    with a `messages.create(to=..., from_=..., body=...)` method.

    `business_name` and `business_short_name` are interpolated into the
    customer-confirmation and owner-emergency templates, so the same code
    path works for any client (defaults preserve current Clear Slope copy
    if the caller forgets to pass them).
    """

    def __init__(
        self,
        *,
        client: _SupportsMessages,
        from_number: str,
        owner_number: str = "",
        business_name: str = "Clear Slope Pest Control",
        business_short_name: str = "Clear Slope",
    ) -> None:
        self._client = client
        self.from_number = from_number
        self.owner_number = owner_number
        self.business_name = business_name
        self.business_short_name = business_short_name

    async def send_customer_confirmation(
        self, *, to: str, caller_name: str, urgency: str
    ) -> bool:
        """Send a "we got your request" SMS to the caller. Returns True on success."""
        if not to or to.strip().lower() in _ANONYMOUS_FROM_VALUES:
            return False
        if not self.from_number:
            logger.warning("customer SMS skipped: TWILIO_SMS_FROM not configured")
            return False
        body = self._customer_message(caller_name=caller_name, urgency=urgency)
        return await self._send(to=to, body=body, label="customer_confirmation")

    async def send_owner_emergency_alert(
        self,
        *,
        caller_name: str,
        address: str,
        pest: str,
        callback: str,
    ) -> bool:
        """Notify the owner about an emergency-urgency intake. Returns True on success."""
        if not self.owner_number:
            return False
        if not self.from_number:
            logger.warning("owner alert skipped: TWILIO_SMS_FROM not configured")
            return False
        body = (
            f"EMERGENCY intake, {self.business_short_name}\n"
            f"Name: {caller_name or 'unknown'}\n"
            f"Phone: {callback or 'unknown'}\n"
            f"Address: {address or 'unknown'}\n"
            f"Pest: {pest or 'unknown'}"
        )
        return await self._send(to=self.owner_number, body=body, label="owner_emergency")

    async def send_voicemail_alert(
        self,
        *,
        caller_number: str,
        duration_seconds: int,
        transcript: str,
        playback_url: str,
    ) -> bool:
        """Notify the owner about a new voicemail. Returns True on success."""
        if not self.owner_number:
            return False
        if not self.from_number:
            logger.warning("voicemail alert skipped: TWILIO_SMS_FROM not configured")
            return False
        body = self._voicemail_message(
            caller_number=caller_number,
            duration_seconds=duration_seconds,
            transcript=transcript,
            playback_url=playback_url,
        )
        return await self._send(to=self.owner_number, body=body, label="voicemail_alert")

    def _voicemail_message(
        self,
        *,
        caller_number: str,
        duration_seconds: int,
        transcript: str,
        playback_url: str,
    ) -> str:
        if caller_number and caller_number.strip().lower() not in _ANONYMOUS_FROM_VALUES:
            last4 = caller_number[-4:] if len(caller_number) >= 4 else caller_number
            from_label = f"***{last4}"
        else:
            from_label = "<anonymous>"
        mins, secs = divmod(max(0, int(duration_seconds)), 60)
        duration = f"{mins}:{secs:02d}"
        text = transcript.strip() if transcript else ""
        if not text:
            text = "(no transcript available)"
        elif len(text) > 300:
            text = text[:300] + "…"
        return (
            f"New voicemail, {self.business_short_name}\n"
            f"From: {from_label}  ({duration})\n"
            f"\"{text}\"\n"
            f"Listen: {playback_url}"
        )

    async def _send(self, *, to: str, body: str, label: str) -> bool:
        try:
            await asyncio.to_thread(
                self._client.messages.create,
                to=to,
                from_=self.from_number,
                body=body,
            )
        except TwilioRestException as exc:
            logger.warning("sms %s failed: status=%s msg=%s", label, exc.status, exc.msg)
            return False
        except Exception:
            logger.exception("sms %s failed unexpectedly", label)
            return False
        last4 = to[-4:] if len(to) >= 4 else to
        logger.info("sms %s sent to=***%s", label, last4)
        return True

    def _customer_message(self, *, caller_name: str, urgency: str) -> str:
        first = (caller_name.strip().split() or ["there"])[0] if caller_name else "there"
        timing = {
            "emergency": "shortly",
            "soon": "today",
        }.get((urgency or "").strip().lower(), "by the next business day")
        return (
            f"Hi {first}, thanks for calling {self.business_name}. "
            f"We received your request and will follow up {timing}. "
            f", {self.business_short_name}. Reply STOP to opt out."
        )


def notifier_from_env(
    *,
    prefix: str,
    business_name: str,
    business_short_name: str,
) -> SmsNotifier | None:
    """Build an `SmsNotifier` for one client from environment variables.

    Required: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN (global), {prefix}_TWILIO_SMS_FROM.
    Optional: {prefix}_OWNER_NOTIFY_NUMBER.

    TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are shared across clients (D-11).
    {prefix}_TWILIO_SMS_FROM and {prefix}_OWNER_NOTIFY_NUMBER are per-client (D-06).
    Returns None if required vars are missing.
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.environ.get(f"{prefix}_TWILIO_SMS_FROM", "").strip()
    owner_number = os.environ.get(f"{prefix}_OWNER_NOTIFY_NUMBER", "").strip()
    if not (account_sid and auth_token and from_number):
        return None
    client = TwilioClient(account_sid, auth_token)
    return SmsNotifier(
        client=client,
        from_number=from_number,
        owner_number=owner_number,
        business_name=business_name,
        business_short_name=business_short_name,
    )
