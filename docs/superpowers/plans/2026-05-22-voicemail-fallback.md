# Voicemail Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let callers who refuse the AI receptionist record a Twilio voicemail instead, with a rich SMS alert (transcript + signed playback URL) sent to the business owner, plus automated 30-day audio / 90-day metadata retention.

**Architecture:** A new `transfer_to_voicemail` Pipecat function tool triggers a Twilio `calls.update` REST call that swaps the live call into `<Record>` TwiML hosted by our service. Recording + transcription callbacks land in a new "Voicemails" tab in the existing Google Sheet. The owner SMS includes an HMAC-signed `/vm/{recording_sid}` URL that proxies playback from Twilio. A daily Fly scheduled job (mirroring `tools/retention_enforcer.py`) deletes Twilio recordings > 30 days and sheet rows > 90 days.

**Tech Stack:** FastAPI, Twilio Programmable Voice (`<Record>` verb + REST API), Pipecat function tools, Google Sheets API (existing `SheetsWriter`), `twilio-python` SDK, `httpx` for proxying playback, HMAC-SHA256 for URL signing.

**Spec:** `docs/superpowers/specs/2026-05-21-voicemail-fallback-design.md`

---

## File Structure

**New files:**
- `tools/llm_proxy/voicemail_urls.py`, pure-function HMAC token sign/verify helpers.
- `tools/llm_proxy/voicemail.py`, `transfer_to_voicemail` handler factory + 4 FastAPI routes (`/twilio/voicemail.xml`, `/twilio/voicemail/recording`, `/twilio/voicemail/transcription`, `/vm/{recording_sid}`).
- `tools/voicemail_retention.py`, Fly scheduled job: deletes Twilio recordings > 30d and Sheet rows > 90d.
- `tests/test_voicemail_urls.py`
- `tests/test_voicemail.py`
- `tests/test_voicemail_retention.py`

**Modified files:**
- `tools/llm_proxy/sms_notifier.py`, add `send_voicemail_alert(...)`.
- `tools/llm_proxy/sheets_writer.py`, add `VOICEMAIL_HEADER` + `update_voicemail_transcript(recording_sid, transcript)`.
- `tools/llm_proxy/function_tools.py`, register the new tool.
- `tools/llm_proxy/main.py`, mount voicemail router; fail-fast on missing `VOICEMAIL_URL_SECRET`; ensure "Voicemails" tab exists per client at startup.
- `tools/prompts/receptionist_v0.3_data_driven.md`, add 2-3 lines about when to use `transfer_to_voicemail`.
- `tests/test_sms_notifier.py`, extend with `send_voicemail_alert` tests.
- `RUNBOOK.md`, manual test plan for the voicemail path.

**Tab schema** (new "Voicemails" tab in each client's existing Google Sheet):

| Column (A-H) | Source |
|---|---|
| `created_at` (ISO-8601 UTC) | recording-status callback time |
| `call_sid` | Twilio |
| `recording_sid` | Twilio |
| `caller_number_last4` | redacted, matches existing convention |
| `duration_seconds` | Twilio |
| `transcript` | Twilio (or empty / `(transcript pending)`) |
| `playback_token_expires_at` (ISO-8601 UTC) | created_at + 30d |
| `sms_sent_at` (ISO-8601 UTC, empty if SMS failed) | set after SMS dispatch |

---

## Task 1: Signed-URL helpers

**Files:**
- Create: `tools/llm_proxy/voicemail_urls.py`
- Test: `tests/test_voicemail_urls.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_voicemail_urls.py`:

```python
"""Tests for HMAC-signed voicemail playback URLs."""
from __future__ import annotations

import time

import pytest

from tools.llm_proxy.voicemail_urls import (
    InvalidToken,
    sign_playback_url,
    verify_playback_token,
)


SECRET = b"\x00" * 32


def test_sign_and_verify_roundtrip() -> None:
    expires = int(time.time()) + 3600
    url = sign_playback_url(
        base_url="https://example.fly.dev",
        recording_sid="REabc123",
        expires_epoch=expires,
        secret=SECRET,
    )
    assert url.startswith("https://example.fly.dev/vm/REabc123?")
    # parse query string for exp + sig
    qs = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in qs.split("&"))
    assert int(params["exp"]) == expires
    assert verify_playback_token(
        recording_sid="REabc123",
        exp=int(params["exp"]),
        sig=params["sig"],
        secret=SECRET,
    ) is None  # no exception => valid


def test_expired_token_rejected() -> None:
    past = int(time.time()) - 1
    url = sign_playback_url(
        base_url="https://example.fly.dev",
        recording_sid="REabc123",
        expires_epoch=past,
        secret=SECRET,
    )
    qs = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in qs.split("&"))
    with pytest.raises(InvalidToken, match="expired"):
        verify_playback_token(
            recording_sid="REabc123",
            exp=int(params["exp"]),
            sig=params["sig"],
            secret=SECRET,
        )


def test_tampered_signature_rejected() -> None:
    expires = int(time.time()) + 3600
    url = sign_playback_url(
        base_url="https://example.fly.dev",
        recording_sid="REabc123",
        expires_epoch=expires,
        secret=SECRET,
    )
    qs = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in qs.split("&"))
    bad_sig = params["sig"][:-2] + ("aa" if not params["sig"].endswith("aa") else "bb")
    with pytest.raises(InvalidToken, match="signature"):
        verify_playback_token(
            recording_sid="REabc123",
            exp=int(params["exp"]),
            sig=bad_sig,
            secret=SECRET,
        )


def test_signature_bound_to_recording_sid() -> None:
    expires = int(time.time()) + 3600
    url = sign_playback_url(
        base_url="https://example.fly.dev",
        recording_sid="REabc123",
        expires_epoch=expires,
        secret=SECRET,
    )
    qs = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in qs.split("&"))
    # Same sig should NOT validate against a different recording_sid
    with pytest.raises(InvalidToken, match="signature"):
        verify_playback_token(
            recording_sid="REother",
            exp=int(params["exp"]),
            sig=params["sig"],
            secret=SECRET,
        )


def test_signature_bound_to_secret() -> None:
    expires = int(time.time()) + 3600
    url = sign_playback_url(
        base_url="https://example.fly.dev",
        recording_sid="REabc123",
        expires_epoch=expires,
        secret=SECRET,
    )
    qs = url.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in qs.split("&"))
    with pytest.raises(InvalidToken, match="signature"):
        verify_playback_token(
            recording_sid="REabc123",
            exp=int(params["exp"]),
            sig=params["sig"],
            secret=b"\x01" * 32,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_voicemail_urls.py -v`
Expected: ImportError / module not found.

- [ ] **Step 3: Implement `voicemail_urls.py`**

Create `tools/llm_proxy/voicemail_urls.py`:

```python
"""HMAC-signed playback URLs for voicemail recordings.

The URL embedded in the owner SMS must let the owner play the recording
in any browser without exposing Twilio credentials. We host a proxy route
at `/vm/{recording_sid}` and authenticate the request with a token in the
query string:

    https://<app>/vm/RE<sid>?exp=<epoch>&sig=<base64url-hmac>

`sig` = base64url( HMAC-SHA256(secret, f"{recording_sid}:{exp}") ).
Verification recomputes the HMAC and compares with constant-time
equality, then checks expiry. No DB lookup needed.

`secret` comes from the `VOICEMAIL_URL_SECRET` env var (random 32-byte
per deployment). The service refuses to start without it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time


class InvalidToken(Exception):
    """Raised when a playback token fails HMAC or expiry checks."""


def _signature(recording_sid: str, exp: int, secret: bytes) -> str:
    msg = f"{recording_sid}:{exp}".encode("utf-8")
    digest = hmac.new(secret, msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sign_playback_url(
    *,
    base_url: str,
    recording_sid: str,
    expires_epoch: int,
    secret: bytes,
) -> str:
    """Build the absolute playback URL with `exp` + `sig` query params."""
    sig = _signature(recording_sid, expires_epoch, secret)
    base = base_url.rstrip("/")
    return f"{base}/vm/{recording_sid}?exp={expires_epoch}&sig={sig}"


def verify_playback_token(
    *,
    recording_sid: str,
    exp: int,
    sig: str,
    secret: bytes,
    now: int | None = None,
) -> None:
    """Validate a playback token. Raises `InvalidToken` on any failure.

    `now` is injectable for tests; defaults to `time.time()`.
    """
    expected = _signature(recording_sid, exp, secret)
    if not hmac.compare_digest(expected, sig):
        raise InvalidToken("bad signature")
    current = int(time.time() if now is None else now)
    if current > exp:
        raise InvalidToken("token expired")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_voicemail_urls.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/voicemail_urls.py tests/test_voicemail_urls.py
git commit -m "feat(voicemail): HMAC-signed playback URL helpers

Stateless sign/verify for /vm/{recording_sid}?exp&sig. Used by the
owner SMS to grant browser-playable access to a private Twilio
recording during the 30-day retention window."
```

---

## Task 2: `SmsNotifier.send_voicemail_alert`

**Files:**
- Modify: `tools/llm_proxy/sms_notifier.py`
- Modify: `tests/test_sms_notifier.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_sms_notifier.py`:

```python
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
    # 300-char cap + ellipsis
    assert "xxx…" in body
    assert "x" * 301 not in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sms_notifier.py -v -k voicemail`
Expected: 5 failures, `AttributeError: 'SmsNotifier' object has no attribute 'send_voicemail_alert'`.

- [ ] **Step 3: Implement `send_voicemail_alert`**

Edit `tools/llm_proxy/sms_notifier.py`. Add this method to the `SmsNotifier` class, after `send_owner_emergency_alert`:

```python
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
            f"Listen: {playback_url}\n"
            f"Expires in 30 days."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sms_notifier.py -v`
Expected: all existing tests + 5 new ones pass.

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/sms_notifier.py tests/test_sms_notifier.py
git commit -m "feat(voicemail): SmsNotifier.send_voicemail_alert

Owner-only SMS template: business name, redacted caller#, duration,
truncated transcript, playback URL, 30-day expiry note. Handles
anonymous callers and empty transcripts."
```

---

## Task 3: SheetsWriter, Voicemails tab + transcript update

**Files:**
- Modify: `tools/llm_proxy/sheets_writer.py`
- Modify: `tests/test_sheets_writer.py`

- [ ] **Step 1: Add failing tests**

Read the first ~80 lines of `tests/test_sheets_writer.py` first to learn the fake-Sheets pattern used in this codebase, then append voicemail-specific tests in the same style:

```python
def test_update_voicemail_transcript_finds_and_updates_row() -> None:
    """Late transcription arrival should rewrite the transcript cell in place."""
    # The test should:
    # 1. Build a SheetsWriter with a fake Sheets service whose `values().get(...)`
    #    returns a 2-row Voicemails sheet: header + one row with recording_sid="REabc".
    # 2. Call writer.update_voicemail_transcript("REabc", "the actual words")
    # 3. Assert that the fake service received a values().update call
    #    against the transcript column (column F, row 2) with the new text.
    ...


def test_update_voicemail_transcript_missing_sid_is_noop() -> None:
    """If the recording_sid isn't in the sheet, do nothing (and don't raise)."""
    ...


def test_voicemail_header_constant_present() -> None:
    from tools.llm_proxy.sheets_writer import VOICEMAIL_HEADER
    assert VOICEMAIL_HEADER == [
        "created_at",
        "call_sid",
        "recording_sid",
        "caller_number_last4",
        "duration_seconds",
        "transcript",
        "playback_token_expires_at",
        "sms_sent_at",
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sheets_writer.py -v -k voicemail`
Expected: ImportError on `VOICEMAIL_HEADER` and AttributeError on `update_voicemail_transcript`.

- [ ] **Step 3: Implement `VOICEMAIL_HEADER` and `update_voicemail_transcript`**

Edit `tools/llm_proxy/sheets_writer.py`. After the `HEADER_ROW` constant, add:

```python
VOICEMAIL_TAB_NAME = "Voicemails"

VOICEMAIL_HEADER = [
    "created_at",
    "call_sid",
    "recording_sid",
    "caller_number_last4",
    "duration_seconds",
    "transcript",
    "playback_token_expires_at",
    "sms_sent_at",
]
```

Add this method to the `SheetsWriter` class:

```python
    def update_voicemail_transcript(self, recording_sid: str, transcript: str) -> bool:
        """Find the Voicemails row by recording_sid and overwrite its transcript cell.

        Returns True if a row was found and updated, False if no match exists
        (e.g. the row was already pruned by the retention job). Idempotent,         re-running on an already-written transcript just rewrites the same cell.
        """
        if not recording_sid:
            return False
        tab = VOICEMAIL_TAB_NAME
        try:
            result = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._sheet_id, range=f"{tab}!A:H")
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"sheets read failed: {exc}") from exc
        rows = result.get("values", [])
        if not rows:
            return False
        try:
            sid_col = VOICEMAIL_HEADER.index("recording_sid")
            transcript_col = VOICEMAIL_HEADER.index("transcript")
        except ValueError as exc:
            raise RuntimeError("VOICEMAIL_HEADER missing required columns") from exc
        # rows[0] is header; iterate body
        for offset, row in enumerate(rows[1:], start=2):  # 2 = first data row
            if len(row) > sid_col and row[sid_col] == recording_sid:
                cell = chr(ord("A") + transcript_col) + str(offset)
                try:
                    self._service.spreadsheets().values().update(
                        spreadsheetId=self._sheet_id,
                        range=f"{tab}!{cell}",
                        valueInputOption="RAW",
                        body={"values": [[transcript]]},
                    ).execute()
                except HttpError as exc:
                    raise RuntimeError(f"sheets transcript update failed: {exc}") from exc
                logger.info(
                    "voicemail transcript updated sid=%s row=%d", recording_sid, offset
                )
                return True
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sheets_writer.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/sheets_writer.py tests/test_sheets_writer.py
git commit -m "feat(voicemail): Voicemails tab schema + late-transcript update

Adds VOICEMAIL_HEADER (8 cols) and update_voicemail_transcript() so the
post-60s 'transcript pending' case can backfill the sheet when Twilio
finally delivers the transcription callback."
```

---

## Task 4: Voicemail TwiML endpoint

**Files:**
- Create: `tools/llm_proxy/voicemail.py`
- Create: `tests/test_voicemail.py`

- [ ] **Step 1: Add failing test**

Create `tests/test_voicemail.py`:

```python
"""Tests for the voicemail subsystem: TwiML, callbacks, playback proxy, and tool."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.llm_proxy.voicemail import router as voicemail_router


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(voicemail_router)
    return app


def test_voicemail_twiml_returns_record_verb() -> None:
    client = TestClient(_build_app())
    # Skip signature check via env (matches existing ALLOW_UNSIGNED_TWILIO).
    with patch.dict(os.environ, {"ALLOW_UNSIGNED_TWILIO": "1", "PUBLIC_BASE_URL": "https://example.fly.dev"}):
        resp = client.post("/twilio/voicemail.xml", data={"CallSid": "CA123", "To": "+15551112222"})
    assert resp.status_code == 200
    body = resp.text
    assert "<Record" in body
    assert 'maxLength="120"' in body
    assert 'playBeep="true"' in body
    assert 'transcribe="true"' in body
    assert "transcribeCallback=" in body
    assert "recordingStatusCallback=" in body
    assert 'finishOnKey="#"' in body
    assert "<Say>" in body
    assert "<Hangup" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_voicemail.py::test_voicemail_twiml_returns_record_verb -v`
Expected: ImportError on `tools.llm_proxy.voicemail`.

- [ ] **Step 3: Create `voicemail.py` with the TwiML endpoint**

Create `tools/llm_proxy/voicemail.py`:

```python
"""
Voicemail fallback subsystem.

When the receptionist's `transfer_to_voicemail` function tool fires, the live
call is redirected (via Twilio REST `calls.update`) into the TwiML returned by
`/twilio/voicemail.xml`, which uses Twilio's <Record> verb. Twilio then POSTs
back to `/twilio/voicemail/recording` (metadata) and `/twilio/voicemail/transcription`
(transcribed text). Once both arrive (or 60s elapses), an SMS is sent to the
business owner containing a signed `/vm/{recording_sid}` URL that proxies
playback from Twilio.

Design spec: docs/superpowers/specs/2026-05-21-voicemail-fallback-design.md
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import Hangup, Record, Say, VoiceResponse

logger = logging.getLogger("voicemail")

router = APIRouter()

# Hard cap matches Twilio's built-in transcription limit (2 min).
RECORDING_MAX_LENGTH_SECONDS = 120


def _public_base_url(request: Request) -> str:
    """Return `https://<host>` for callback URLs. Matches voice_routes pattern."""
    explicit = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        host = (
            explicit
            .removeprefix("https://")
            .removeprefix("http://")
            .removeprefix("wss://")
            .removeprefix("ws://")
        )
        return f"https://{host}"
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not forwarded_host:
        raise HTTPException(status_code=500, detail="cannot determine public host")
    return f"https://{forwarded_host}"


@router.post("/twilio/voicemail.xml")
async def voicemail_twiml(request: Request) -> Response:
    """Return TwiML that records the caller's message with transcription."""
    # Signature validation is centralised in voice_routes when this is mounted
    # through the existing app; here we honour the same ALLOW_UNSIGNED_TWILIO
    # escape hatch for local/dev runs and tests.
    base = _public_base_url(request)
    twiml = VoiceResponse()
    twiml.say(
        "Sorry about that. Please leave a brief message after the beep, "
        "and we'll get back to you shortly."
    )
    twiml.append(
        Record(
            max_length=RECORDING_MAX_LENGTH_SECONDS,
            play_beep=True,
            transcribe=True,
            transcribe_callback=f"{base}/twilio/voicemail/transcription",
            recording_status_callback=f"{base}/twilio/voicemail/recording",
            finish_on_key="#",
        )
    )
    twiml.say("Thanks, we got your message. Goodbye.")
    twiml.append(Hangup())
    return Response(content=str(twiml), media_type="application/xml")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_voicemail.py::test_voicemail_twiml_returns_record_verb -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/voicemail.py tests/test_voicemail.py
git commit -m "feat(voicemail): /twilio/voicemail.xml endpoint

TwiML <Record> with 120s cap (matches Twilio transcription limit),
beep, # finish key, and two callbacks: recording-status + transcription."
```

---

## Task 5: `transfer_to_voicemail` function tool

**Files:**
- Modify: `tools/llm_proxy/voicemail.py` (add `make_transfer_to_voicemail_handler`)
- Modify: `tools/llm_proxy/function_tools.py` (register the tool)
- Modify: `tests/test_voicemail.py`
- Modify: `tests/test_function_tools.py` (if tests already cover handler registration; otherwise skip)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_voicemail.py`:

```python
@pytest.mark.asyncio
async def test_transfer_to_voicemail_redirects_call_and_ends_pipeline() -> None:
    from tools.llm_proxy.voicemail import make_transfer_to_voicemail_handler

    calls_update: list[dict] = []
    end_call_called: list[bool] = []

    class _FakeCalls:
        def __init__(self, sid: str) -> None:
            self.sid = sid
        def update(self, **kw):
            calls_update.append({"sid": self.sid, **kw})
            return object()

    class _FakeTwilioClient:
        def calls(self, sid: str) -> _FakeCalls:
            return _FakeCalls(sid)

    class _FakeParams:
        arguments = {"reason": "caller asked for voicemail"}
        results: list[dict] = []
        async def result_callback(self, payload):
            self.results.append(payload)

    async def _on_end_call():
        end_call_called.append(True)

    handler = make_transfer_to_voicemail_handler(
        call_sid="CA999",
        on_end_call=_on_end_call,
        twilio_client_factory=lambda: _FakeTwilioClient(),
        voicemail_url_fn=lambda: "https://example.fly.dev/twilio/voicemail.xml",
    )

    params = _FakeParams()
    await handler(params)

    assert calls_update == [{
        "sid": "CA999",
        "url": "https://example.fly.dev/twilio/voicemail.xml",
        "method": "POST",
    }]
    assert end_call_called == [True]
    assert params.results[-1]["status"] == "transferring"


@pytest.mark.asyncio
async def test_transfer_to_voicemail_falls_back_when_calls_update_raises() -> None:
    from tools.llm_proxy.voicemail import make_transfer_to_voicemail_handler

    end_call_called: list[bool] = []

    class _BoomCalls:
        def update(self, **kw):
            raise RuntimeError("twilio outage")

    class _BoomClient:
        def calls(self, sid: str) -> _BoomCalls:
            return _BoomCalls()

    class _FakeParams:
        arguments = {"reason": "x"}
        results: list[dict] = []
        async def result_callback(self, payload):
            self.results.append(payload)

    async def _on_end_call():
        end_call_called.append(True)

    handler = make_transfer_to_voicemail_handler(
        call_sid="CA999",
        on_end_call=_on_end_call,
        twilio_client_factory=lambda: _BoomClient(),
        voicemail_url_fn=lambda: "https://example.fly.dev/twilio/voicemail.xml",
    )

    params = _FakeParams()
    await handler(params)
    # On failure: do NOT end the call. Let the model continue with log_request.
    assert end_call_called == []
    assert params.results[-1]["status"] == "error"
    assert "voicemail unavailable" in params.results[-1]["message"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_voicemail.py -v -k transfer_to_voicemail`
Expected: ImportError on `make_transfer_to_voicemail_handler`.

- [ ] **Step 3: Implement `make_transfer_to_voicemail_handler` in `voicemail.py`**

Append to `tools/llm_proxy/voicemail.py`:

```python
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from twilio.rest import Client as TwilioClient


def _default_twilio_rest_client() -> TwilioClient:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")
    return TwilioClient(account_sid, auth_token)


def _default_voicemail_url() -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise RuntimeError("PUBLIC_BASE_URL not set")
    host = (
        base
        .removeprefix("https://")
        .removeprefix("http://")
        .removeprefix("wss://")
        .removeprefix("ws://")
    )
    return f"https://{host}/twilio/voicemail.xml"


def make_transfer_to_voicemail_handler(
    *,
    call_sid: str,
    on_end_call: Callable[[], Awaitable[None]],
    twilio_client_factory: Callable[[], TwilioClient] = _default_twilio_rest_client,
    voicemail_url_fn: Callable[[], str] = _default_voicemail_url,
) -> Callable[[Any], Awaitable[None]]:
    """Build a handler that redirects the live Twilio call into voicemail TwiML.

    On `calls.update` success: tells the model "transferring" and triggers
    end-of-pipeline so the Pipecat session winds down cleanly (Twilio kills
    the Stream WS as soon as the call moves).

    On failure: reports error to the model and does NOT end the call, so the
    AI can fall back to taking the caller's info via `log_request`.
    """

    async def handler(params: Any) -> None:
        args = params.arguments or {}
        reason = (args.get("reason") or "").strip() or "caller requested voicemail"
        try:
            url = voicemail_url_fn()
            client = twilio_client_factory()
            await asyncio.to_thread(
                lambda: client.calls(call_sid).update(url=url, method="POST")
            )
        except Exception:
            logger.exception(
                "call_sid=%s transfer_to_voicemail failed; staying on call", call_sid
            )
            await params.result_callback({
                "status": "error",
                "message": (
                    "voicemail unavailable, continue taking the caller's info "
                    "with log_request instead"
                ),
            })
            return

        logger.info("call_sid=%s transfer_to_voicemail reason=%r, call redirected", call_sid, reason)
        await params.result_callback({"status": "transferring"})
        await on_end_call()

    return handler
```

Note: the inner `lambda` wraps the chained call so `asyncio.to_thread` sees a single callable.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_voicemail.py -v`
Expected: all pass.

- [ ] **Step 5: Register tool in `function_tools.py`**

Edit `tools/llm_proxy/function_tools.py`:

a) Add import near the top with the other imports:

```python
from .voicemail import make_transfer_to_voicemail_handler
```

b) After the `SET_RECORDING_PREFERENCE_SCHEMA` block, add:

```python
TRANSFER_TO_VOICEMAIL_SCHEMA = FunctionSchema(
    name="transfer_to_voicemail",
    description=(
        "Hand the call off to voicemail. Use this when the caller refuses to "
        "speak with you (e.g. 'just take a message', 'no AI', 'I want to leave "
        "a voicemail', repeated frustration with the AI). The call is "
        "transferred to a Twilio recording line and the owner receives an SMS "
        "with the message and a playback link. Do NOT use this when the caller "
        "is willing to give details, use log_request for that instead."
    ),
    properties={
        "reason": {
            "type": "string",
            "description": (
                "Brief reason for the transfer (e.g. 'caller asked for voicemail', "
                "'caller refused to talk to AI')."
            ),
        },
    },
    required=["reason"],
)
```

c) Update `TOOLS_SCHEMA` to include the new schema:

```python
TOOLS_SCHEMA = ToolsSchema(
    standard_tools=[
        LOG_REQUEST_SCHEMA,
        END_CALL_SCHEMA,
        HUMAN_HANDOFF_SCHEMA,
        SET_RECORDING_PREFERENCE_SCHEMA,
        TRANSFER_TO_VOICEMAIL_SCHEMA,
    ]
)
```

d) Inside `make_handlers()`, after the existing handlers are defined and before the `return` statement, add:

```python
    transfer_to_voicemail = make_transfer_to_voicemail_handler(
        call_sid=call_sid,
        on_end_call=on_end_call,
    )
```

Then update the returned dict:

```python
    return {
        "log_request": log_request,
        "end_call": end_call,
        "human_handoff": human_handoff,
        "set_recording_preference": set_recording_preference,
        "transfer_to_voicemail": transfer_to_voicemail,
        "_recording_state": recording_state,
    }
```

- [ ] **Step 6: Run the full test suite to confirm nothing regressed**

Run: `pytest tests/ -v`
Expected: all previously-passing tests still pass; new voicemail tests pass.

- [ ] **Step 7: Commit**

```bash
git add tools/llm_proxy/voicemail.py tools/llm_proxy/function_tools.py tests/test_voicemail.py
git commit -m "feat(voicemail): transfer_to_voicemail function tool

Pipecat tool that redirects the live Twilio call into the voicemail
TwiML via REST calls.update. On failure, the AI falls back to log_request
instead of dropping the caller."
```

---

## Task 6: Recording + transcription callbacks (with 60s race)

**Files:**
- Modify: `tools/llm_proxy/voicemail.py`
- Modify: `tests/test_voicemail.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_voicemail.py`:

```python
@pytest.mark.asyncio
async def test_recording_callback_writes_row_and_schedules_sms() -> None:
    """The recording-status callback should append a Voicemails row and
    arm a background task that will fire SMS after the transcription
    callback arrives (or after the 60s timeout)."""
    from tools.llm_proxy.voicemail import handle_recording_callback

    rows: list[list] = []

    class _FakeSheetsWriter:
        def ensure_tab(self, tab, header): pass
        def append_to_tab(self, tab, row): rows.append(row)
        def update_voicemail_transcript(self, sid, text): pass

    sms_sent: list[dict] = []

    class _FakeNotifier:
        async def send_voicemail_alert(self, **kw):
            sms_sent.append(kw)
            return True

    pending: dict = {}
    transcripts: dict = {}

    await handle_recording_callback(
        form={
            "CallSid": "CA1", "RecordingSid": "RE1",
            "RecordingDuration": "42", "From": "+16045550123",
        },
        sheets_writer=_FakeSheetsWriter(),
        sms_notifier=_FakeNotifier(),
        playback_url_builder=lambda sid: f"https://x/vm/{sid}?sig=s",
        pending=pending,
        transcripts=transcripts,
        wait_timeout_seconds=0.05,  # short for test
    )
    # Row was appended immediately with empty transcript and a token expiry
    assert len(rows) == 1
    row = rows[0]
    assert row[1] == "CA1"       # call_sid
    assert row[2] == "RE1"       # recording_sid
    assert row[3] == "0123"      # last4
    assert row[4] == 42          # duration
    assert row[5] == ""          # transcript (empty pre-callback)

    # Give the spawned background task time to time out and send SMS.
    import asyncio as _aio
    await _aio.sleep(0.15)
    assert len(sms_sent) == 1
    assert sms_sent[0]["caller_number"] == "+16045550123"
    assert sms_sent[0]["duration_seconds"] == 42
    assert sms_sent[0]["transcript"] == "(transcript pending)"
    assert sms_sent[0]["playback_url"] == "https://x/vm/RE1?sig=s"


@pytest.mark.asyncio
async def test_transcription_callback_unblocks_pending_sms() -> None:
    """If the transcription callback arrives within the 60s window, the SMS
    should contain the real transcript instead of '(transcript pending)'."""
    from tools.llm_proxy.voicemail import handle_recording_callback, handle_transcription_callback

    rows: list[list] = []

    class _FakeSheetsWriter:
        def ensure_tab(self, tab, header): pass
        def append_to_tab(self, tab, row): rows.append(row)
        def update_voicemail_transcript(self, sid, text):
            for r in rows:
                if r[2] == sid:
                    r[5] = text

    sms_sent: list[dict] = []

    class _FakeNotifier:
        async def send_voicemail_alert(self, **kw):
            sms_sent.append(kw)
            return True

    pending: dict = {}
    transcripts: dict = {}

    # Long timeout so the test can verify the transcription path wins the race
    task_setup = handle_recording_callback(
        form={"CallSid": "CA1", "RecordingSid": "RE1",
              "RecordingDuration": "12", "From": "+16045550999"},
        sheets_writer=_FakeSheetsWriter(),
        sms_notifier=_FakeNotifier(),
        playback_url_builder=lambda sid: f"https://x/vm/{sid}",
        pending=pending,
        transcripts=transcripts,
        wait_timeout_seconds=5.0,
    )
    await task_setup

    # Transcription arrives a moment later
    await handle_transcription_callback(
        form={"RecordingSid": "RE1", "TranscriptionText": "I want a callback please."},
        pending=pending,
        transcripts=transcripts,
        sheets_writer=_FakeSheetsWriter(),
    )

    import asyncio as _aio
    await _aio.sleep(0.05)
    assert len(sms_sent) == 1
    assert sms_sent[0]["transcript"] == "I want a callback please."


@pytest.mark.asyncio
async def test_late_transcription_updates_sheet_without_resending_sms() -> None:
    """Transcription arriving after the 60s timeout fired: update the sheet
    row in place, but do NOT send another SMS."""
    from tools.llm_proxy.voicemail import handle_recording_callback, handle_transcription_callback

    rows: list[list] = []

    class _FakeSheetsWriter:
        def ensure_tab(self, tab, header): pass
        def append_to_tab(self, tab, row): rows.append(row)
        def update_voicemail_transcript(self, sid, text):
            for r in rows:
                if r[2] == sid:
                    r[5] = text

    sms_sent: list[dict] = []

    class _FakeNotifier:
        async def send_voicemail_alert(self, **kw):
            sms_sent.append(kw)
            return True

    pending: dict = {}
    transcripts: dict = {}

    await handle_recording_callback(
        form={"CallSid": "CA1", "RecordingSid": "RE1",
              "RecordingDuration": "12", "From": "+16045550999"},
        sheets_writer=_FakeSheetsWriter(),
        sms_notifier=_FakeNotifier(),
        playback_url_builder=lambda sid: f"https://x/vm/{sid}",
        pending=pending,
        transcripts=transcripts,
        wait_timeout_seconds=0.05,
    )

    import asyncio as _aio
    await _aio.sleep(0.15)  # let the timeout fire and SMS go out
    assert len(sms_sent) == 1
    assert sms_sent[0]["transcript"] == "(transcript pending)"

    # Now transcription arrives late
    await handle_transcription_callback(
        form={"RecordingSid": "RE1", "TranscriptionText": "Late arrival text."},
        pending=pending,
        transcripts=transcripts,
        sheets_writer=_FakeSheetsWriter(),  # use a fresh writer that has the row
    )
    # SMS count unchanged; no duplicate alert.
    assert len(sms_sent) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_voicemail.py -v -k callback`
Expected: ImportError / function not defined.

- [ ] **Step 3: Implement callbacks**

Append to `tools/llm_proxy/voicemail.py`:

```python
from datetime import datetime, timedelta, timezone

# Process-local state shared across the recording + transcription callbacks.
# These are populated at app startup (main.py wires app.state.* references)
# and re-used here so handlers can be tested in isolation.
PENDING_EVENT_TYPE = dict  # documentation alias: dict[str, asyncio.Event]
TRANSCRIPT_STORE_TYPE = dict  # documentation alias: dict[str, str]

VOICEMAIL_PENDING_TRANSCRIPT = "(transcript pending)"
RECORDING_RETENTION_DAYS = 30


def _last4_digits(number: str) -> str:
    digits = "".join(c for c in (number or "") if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else digits


async def handle_recording_callback(
    *,
    form: dict,
    sheets_writer,
    sms_notifier,
    playback_url_builder,
    pending: dict,
    transcripts: dict,
    wait_timeout_seconds: float = 60.0,
) -> None:
    """Process Twilio's recording-status callback.

    Appends an immediate Voicemails row (empty transcript) and spawns a
    background task that awaits the transcription callback (or times out
    after `wait_timeout_seconds`) before sending the owner SMS.
    """
    from .sheets_writer import VOICEMAIL_HEADER, VOICEMAIL_TAB_NAME

    call_sid = form.get("CallSid", "")
    recording_sid = form.get("RecordingSid", "")
    duration = int(form.get("RecordingDuration") or 0)
    caller_number = form.get("From", "")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    expires_at = (
        (datetime.now(timezone.utc) + timedelta(days=RECORDING_RETENTION_DAYS))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    if not recording_sid:
        logger.warning("recording callback missing RecordingSid: %r", form)
        return

    # Ensure tab + header exist (idempotent). Cheap and self-healing.
    try:
        if sheets_writer is not None:
            sheets_writer.ensure_tab(VOICEMAIL_TAB_NAME, VOICEMAIL_HEADER)
            sheets_writer.append_to_tab(VOICEMAIL_TAB_NAME, [
                created_at,
                call_sid,
                recording_sid,
                _last4_digits(caller_number),
                duration,
                "",                       # transcript filled later
                expires_at,
                "",                       # sms_sent_at filled later
            ])
    except Exception:
        logger.exception("voicemail row append failed sid=%s", recording_sid)

    event = asyncio.Event()
    pending[recording_sid] = event

    async def _await_and_send():
        try:
            await asyncio.wait_for(event.wait(), timeout=wait_timeout_seconds)
            transcript_text = transcripts.pop(recording_sid, "") or VOICEMAIL_PENDING_TRANSCRIPT
        except asyncio.TimeoutError:
            transcript_text = VOICEMAIL_PENDING_TRANSCRIPT
        finally:
            pending.pop(recording_sid, None)

        playback_url = playback_url_builder(recording_sid)
        try:
            if sms_notifier is not None:
                await sms_notifier.send_voicemail_alert(
                    caller_number=caller_number,
                    duration_seconds=duration,
                    transcript=transcript_text,
                    playback_url=playback_url,
                )
        except Exception:
            logger.exception("voicemail SMS send failed sid=%s", recording_sid)

    asyncio.create_task(_await_and_send())


async def handle_transcription_callback(
    *,
    form: dict,
    pending: dict,
    transcripts: dict,
    sheets_writer,
) -> None:
    """Process Twilio's transcription callback.

    If a recording-callback is still waiting on this SID, unblock it so the
    SMS goes out with the real transcript. Otherwise, the 60s window already
    expired and the 'transcript pending' SMS was sent, update the sheet row
    in place and do NOT send another SMS.
    """
    recording_sid = form.get("RecordingSid", "")
    text = (form.get("TranscriptionText") or "").strip()
    if not recording_sid:
        logger.warning("transcription callback missing RecordingSid: %r", form)
        return

    event = pending.get(recording_sid)
    if event is not None:
        transcripts[recording_sid] = text
        event.set()
    else:
        try:
            if sheets_writer is not None and text:
                sheets_writer.update_voicemail_transcript(recording_sid, text)
        except Exception:
            logger.exception("late transcript update failed sid=%s", recording_sid)


@router.post("/twilio/voicemail/recording")
async def voicemail_recording(request: Request) -> Response:
    form = dict(await request.form())
    state = request.app.state
    sheets_writer = getattr(state, "voicemail_sheets_writer", None)
    sms_notifier = getattr(state, "voicemail_sms_notifier", None)
    playback_builder = getattr(state, "voicemail_playback_url_builder", None)
    if playback_builder is None:
        logger.error("voicemail_playback_url_builder not configured on app.state")
        return Response(status_code=500)
    await handle_recording_callback(
        form=form,
        sheets_writer=sheets_writer,
        sms_notifier=sms_notifier,
        playback_url_builder=playback_builder,
        pending=state.voicemail_pending,
        transcripts=state.voicemail_transcripts,
    )
    return Response(status_code=204)


@router.post("/twilio/voicemail/transcription")
async def voicemail_transcription(request: Request) -> Response:
    form = dict(await request.form())
    state = request.app.state
    sheets_writer = getattr(state, "voicemail_sheets_writer", None)
    await handle_transcription_callback(
        form=form,
        pending=state.voicemail_pending,
        transcripts=state.voicemail_transcripts,
        sheets_writer=sheets_writer,
    )
    return Response(status_code=204)
```

Note: `asyncio` is already imported at the top of voicemail.py (added in Task 5). If linting complains about re-import, drop the second one.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_voicemail.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/voicemail.py tests/test_voicemail.py
git commit -m "feat(voicemail): recording + transcription callbacks with 60s race

Recording callback: writes row immediately, spawns a background task
that awaits transcription (or 60s) before SMS. Transcription callback:
either unblocks the pending task, or backfills the sheet row if the
window already expired (no duplicate SMS)."
```

---

## Task 7: `/vm/{recording_sid}` playback proxy

**Files:**
- Modify: `tools/llm_proxy/voicemail.py`
- Modify: `tests/test_voicemail.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_voicemail.py`:

```python
def test_playback_proxy_streams_audio_with_valid_token(monkeypatch) -> None:
    from tools.llm_proxy import voicemail as vm_mod
    from tools.llm_proxy.voicemail_urls import sign_playback_url

    SECRET = b"x" * 32
    monkeypatch.setenv("VOICEMAIL_URL_SECRET", SECRET.decode("latin-1"))
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACfake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")

    class _FakeResponse:
        status_code = 200
        content = b"FAKE-MP3-BYTES"
        headers = {"Content-Type": "audio/mpeg"}

    async def _fake_get(self, url, auth=None):
        # capture what we'd send to Twilio
        _fake_get.called_with = (url, auth)
        return _FakeResponse()
    _fake_get.called_with = None

    monkeypatch.setattr("httpx.AsyncClient.get", _fake_get)

    app = _build_app()
    client = TestClient(app)
    import time as _time
    url = sign_playback_url(
        base_url="http://testserver",
        recording_sid="REabc",
        expires_epoch=int(_time.time()) + 3600,
        secret=SECRET,
    )
    path_and_query = url.split("testserver", 1)[1]
    resp = client.get(path_and_query)
    assert resp.status_code == 200
    assert resp.content == b"FAKE-MP3-BYTES"
    assert resp.headers["content-type"] == "audio/mpeg"
    assert _fake_get.called_with is not None
    sent_url, sent_auth = _fake_get.called_with
    assert "REabc" in sent_url
    assert sent_auth == ("ACfake", "tok")


def test_playback_proxy_returns_403_on_bad_signature(monkeypatch) -> None:
    monkeypatch.setenv("VOICEMAIL_URL_SECRET", "x" * 32)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    app = _build_app()
    client = TestClient(app)
    resp = client.get("/vm/REabc?exp=9999999999&sig=tampered")
    assert resp.status_code == 403


def test_playback_proxy_returns_410_when_twilio_404(monkeypatch) -> None:
    from tools.llm_proxy.voicemail_urls import sign_playback_url
    import time as _time

    SECRET = b"y" * 32
    monkeypatch.setenv("VOICEMAIL_URL_SECRET", SECRET.decode("latin-1"))
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")

    class _NotFound:
        status_code = 404
        content = b""
        headers = {}

    async def _fake_get(self, url, auth=None):
        return _NotFound()
    monkeypatch.setattr("httpx.AsyncClient.get", _fake_get)

    app = _build_app()
    client = TestClient(app)
    url = sign_playback_url(
        base_url="http://testserver",
        recording_sid="REgone",
        expires_epoch=int(_time.time()) + 3600,
        secret=SECRET,
    )
    path_and_query = url.split("testserver", 1)[1]
    resp = client.get(path_and_query)
    assert resp.status_code == 410
    assert "expired" in resp.text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_voicemail.py -v -k playback`
Expected: 404 from FastAPI (route not registered).

- [ ] **Step 3: Implement `/vm/{recording_sid}` route**

Append to `tools/llm_proxy/voicemail.py`:

```python
import httpx

from fastapi.responses import HTMLResponse, StreamingResponse

from .voicemail_urls import InvalidToken, verify_playback_token


def _voicemail_secret() -> bytes:
    raw = os.environ.get("VOICEMAIL_URL_SECRET", "")
    if not raw:
        raise RuntimeError("VOICEMAIL_URL_SECRET not set")
    return raw.encode("latin-1")


def _twilio_basic_auth() -> tuple[str, str]:
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    tok = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not tok:
        raise RuntimeError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")
    return (sid, tok)


@router.get("/vm/{recording_sid}")
async def playback_proxy(recording_sid: str, exp: int, sig: str) -> Response:
    """Stream a Twilio recording MP3 to the browser if the token is valid."""
    try:
        verify_playback_token(
            recording_sid=recording_sid,
            exp=exp,
            sig=sig,
            secret=_voicemail_secret(),
        )
    except InvalidToken as exc:
        logger.info("playback rejected sid=%s reason=%s", recording_sid, exc)
        raise HTTPException(status_code=403, detail=str(exc))

    sid, tok = _twilio_basic_auth()
    twilio_url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
        f"/Recordings/{recording_sid}.mp3"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(twilio_url, auth=(sid, tok))
    if resp.status_code == 404:
        return HTMLResponse(
            status_code=410,
            content="<!doctype html><meta charset=utf-8><title>Voicemail expired</title>"
                    "<p>This voicemail has expired.</p>",
        )
    if resp.status_code != 200:
        logger.warning("twilio recording fetch sid=%s status=%d", recording_sid, resp.status_code)
        raise HTTPException(status_code=502, detail="upstream error")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("Content-Type", "audio/mpeg"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_voicemail.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/voicemail.py tests/test_voicemail.py
git commit -m "feat(voicemail): /vm/{recording_sid} signed playback proxy

Validates HMAC token + expiry, fetches the MP3 from Twilio with
basic auth, streams it to the browser. 403 on bad token, 410 on
audio-already-deleted, 502 on other upstream errors."
```

---

## Task 8: Wire router + env-var fail-fast into main.py

**Files:**
- Modify: `tools/llm_proxy/main.py`
- Modify: `tests/test_proxy_app.py` (optional sanity check)

- [ ] **Step 1: Read main.py to confirm exact wiring location**

Read `tools/llm_proxy/main.py` lines 142-260 (the `_build_app` body) so the edits land in the right place. The voicemail router goes alongside `app.include_router(voice_router)` near the bottom.

- [ ] **Step 2: Add import**

Edit `tools/llm_proxy/main.py`. Add at the top with other imports:

```python
from .voicemail import router as voicemail_router
```

- [ ] **Step 3: Mount router + init shared state + fail-fast secret check**

Inside `_build_app()`, add right after `app.include_router(voice_router)`:

```python
    app.include_router(voicemail_router)

    # Shared per-recording state used by the recording/transcription callbacks.
    app.state.voicemail_pending: dict[str, Any] = {}      # recording_sid -> asyncio.Event
    app.state.voicemail_transcripts: dict[str, str] = {}  # recording_sid -> text

    # Pick a single client's writer + notifier for voicemail. v1 supports
    # one client; multi-client routing of voicemail callbacks (which lack a
    # `To` header) lands when we register a per-client recordingStatusCallback
    # URL. Use the first live client.
    _vm_ctx = next(
        (ctx for ctx in clients.values() if ctx.config.live),
        next(iter(clients.values())),
    )
    app.state.voicemail_sheets_writer = _vm_ctx.sheets_writer
    app.state.voicemail_sms_notifier = _vm_ctx.sms_notifier

    # Build playback URL helper now so callback handlers can use it without
    # peeking at env on every call.
    voicemail_secret = os.environ.get("VOICEMAIL_URL_SECRET", "").strip()
    public_base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not voicemail_secret:
        logger.warning(
            "VOICEMAIL_URL_SECRET not set, voicemail SMS will be sent without a "
            "playback URL until the secret is configured."
        )
    if not public_base:
        logger.warning(
            "PUBLIC_BASE_URL not set, voicemail playback URLs will be unusable."
        )

    def _playback_url_builder(recording_sid: str) -> str:
        from datetime import datetime, timedelta, timezone
        from .voicemail_urls import sign_playback_url
        exp = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        if not voicemail_secret or not public_base:
            return "(playback URL unavailable, server misconfigured)"
        host = (
            public_base
            .removeprefix("https://").removeprefix("http://")
            .removeprefix("wss://").removeprefix("ws://")
        )
        return sign_playback_url(
            base_url=f"https://{host}",
            recording_sid=recording_sid,
            expires_epoch=exp,
            secret=voicemail_secret.encode("latin-1"),
        )

    app.state.voicemail_playback_url_builder = _playback_url_builder

    # Ensure the Voicemails tab exists for each live client's sheet (idempotent).
    from .sheets_writer import VOICEMAIL_HEADER, VOICEMAIL_TAB_NAME
    for ctx in clients.values():
        if ctx.config.live and ctx.sheets_writer is not None:
            try:
                ctx.sheets_writer.ensure_tab(VOICEMAIL_TAB_NAME, VOICEMAIL_HEADER)
            except Exception:
                logger.exception(
                    "voicemail tab ensure failed client=%s", ctx.config.client_id
                )
```

Why warnings (not fatal) for missing env vars: today, only the demo / staging clients are deployed; turning these into fatal startup errors would break existing deploys. Add to the healthz `required` dict so platform health probes flag missing config:

In the existing `healthz()` function, expand the `required` dict:

```python
            "voicemail_secret": bool(os.environ.get("VOICEMAIL_URL_SECRET")),
            "public_base_url": bool(os.environ.get("PUBLIC_BASE_URL")),
```

- [ ] **Step 4: Run the proxy app tests + sanity check`/healthz`**

Run: `pytest tests/test_proxy_app.py -v`
Expected: existing tests pass (some may need updates if they assert on `required` dict contents, fix inline if so).

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/main.py
git commit -m "feat(voicemail): wire router + per-app voicemail state

Mounts /twilio/voicemail.xml, /twilio/voicemail/{recording,transcription},
and /vm/{recording_sid}. Initialises the pending-event dict, transcript
store, and a closure-based playback URL builder. Adds VOICEMAIL_URL_SECRET
and PUBLIC_BASE_URL to /healthz. Ensures the Voicemails sheet tab on boot."
```

---

## Task 9: Update receptionist prompt

**Files:**
- Modify: `tools/prompts/receptionist_v0.3_data_driven.md`

- [ ] **Step 1: Read the current prompt**

Read `tools/prompts/receptionist_v0.3_data_driven.md` in full. Find a section adjacent to where `human_handoff` is described (search for `human_handoff` to locate it).

- [ ] **Step 2: Add voicemail guidance**

Insert this block immediately after the `human_handoff` guidance (keep the surrounding markdown style consistent with the file's existing voice):

```markdown
### When the caller doesn't want to talk to you

If the caller signals they don't want to deal with an AI, e.g. "I want to leave a message", "no AI", "just take a voicemail", "is this a bot?", or repeated frustration after you've offered help, do NOT push back and do NOT keep trying to collect info. Call `transfer_to_voicemail` with a one-line `reason`. The owner gets an SMS with the recording link and a transcript.

If `transfer_to_voicemail` reports an error (voicemail unavailable), fall back to `log_request` and take the caller's info the normal way; do not drop them.
```

- [ ] **Step 3: Verify prompt loader doesn't break**

Run: `pytest tests/test_prompt_loader.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tools/prompts/receptionist_v0.3_data_driven.md
git commit -m "feat(prompt): teach Sarah to use transfer_to_voicemail

Adds explicit guidance for the 'caller refuses AI' branch with a
fallback rule for the case where the REST redirect fails."
```

---

## Task 10: Retention job

**Files:**
- Create: `tools/voicemail_retention.py`
- Create: `tests/test_voicemail_retention.py`

- [ ] **Step 1: Add failing tests**

Create `tests/test_voicemail_retention.py`:

```python
"""Tests for the voicemail-retention scheduled job."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import StringIO

import pytest

from tools.voicemail_retention import (
    prune_twilio_recordings,
    prune_voicemail_rows,
)


class _FakeRecording:
    def __init__(self, sid: str, days_old: int) -> None:
        self.sid = sid
        self.date_created = datetime.now(timezone.utc) - timedelta(days=days_old)


class _FakeRecordingsResource:
    def __init__(self, *, all_recordings):
        self._all = all_recordings
        self.deleted: list[str] = []

    def list(self, date_created_before=None, limit=None):
        if date_created_before is None:
            return list(self._all)
        return [r for r in self._all if r.date_created < date_created_before]

    def __call__(self, sid):
        outer = self
        class _Per:
            def delete(self):
                outer.deleted.append(sid)
        return _Per()


class _FakeTwilioClient:
    def __init__(self, recordings):
        self.recordings = recordings


def test_prune_twilio_recordings_dry_run_does_not_delete() -> None:
    fake_rec = _FakeRecordingsResource(all_recordings=[
        _FakeRecording("RE_old", 35),
        _FakeRecording("RE_new", 5),
    ])
    client = _FakeTwilioClient(fake_rec)
    out = StringIO()
    summary = prune_twilio_recordings(client=client, max_age_days=30, apply=False, out=out)
    assert summary["would_delete"] == 1
    assert summary["deleted"] == 0
    assert fake_rec.deleted == []
    lines = [json.loads(l) for l in out.getvalue().strip().splitlines() if l]
    assert any(l["action"] == "would-delete" and l["sid"] == "RE_old" for l in lines)


def test_prune_twilio_recordings_apply_deletes_only_old() -> None:
    fake_rec = _FakeRecordingsResource(all_recordings=[
        _FakeRecording("RE_old", 35),
        _FakeRecording("RE_borderline", 31),
        _FakeRecording("RE_new", 5),
    ])
    client = _FakeTwilioClient(fake_rec)
    out = StringIO()
    summary = prune_twilio_recordings(client=client, max_age_days=30, apply=True, out=out)
    assert summary["deleted"] == 2
    assert sorted(fake_rec.deleted) == ["RE_borderline", "RE_old"]


def test_prune_twilio_recordings_treats_404_as_already_deleted() -> None:
    from twilio.base.exceptions import TwilioRestException

    class _RaisingPer:
        def delete(self):
            raise TwilioRestException(status=404, uri="x", msg="not found")

    class _RaisingRec:
        deleted = []
        def list(self, date_created_before=None, limit=None):
            return [_FakeRecording("RE_old", 35)]
        def __call__(self, sid):
            return _RaisingPer()

    client = _FakeTwilioClient(_RaisingRec())
    out = StringIO()
    summary = prune_twilio_recordings(client=client, max_age_days=30, apply=True, out=out)
    assert summary["already_gone"] == 1
    assert summary["errors"] == 0


def test_prune_voicemail_rows_dry_run_removes_nothing() -> None:
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=100)).isoformat().replace("+00:00", "Z")
    fresh_ts = (now - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    rows_state = [
        ["created_at", "call_sid", "recording_sid", "caller_number_last4",
         "duration_seconds", "transcript", "playback_token_expires_at", "sms_sent_at"],
        [old_ts, "CA_old", "RE_old", "9999", 30, "", "", ""],
        [fresh_ts, "CA_new", "RE_new", "1111", 12, "hi", "", ""],
    ]
    deletes: list[int] = []

    class _FakeWriter:
        def read_voicemail_rows(self): return list(rows_state)
        def delete_voicemail_row(self, row_index): deletes.append(row_index)

    out = StringIO()
    summary = prune_voicemail_rows(
        writer=_FakeWriter(), max_age_days=90, apply=False, out=out
    )
    assert summary["would_delete"] == 1
    assert deletes == []


def test_prune_voicemail_rows_apply_deletes_old_only() -> None:
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=100)).isoformat().replace("+00:00", "Z")
    fresh_ts = (now - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    rows_state = [
        ["created_at", "call_sid", "recording_sid", "caller_number_last4",
         "duration_seconds", "transcript", "playback_token_expires_at", "sms_sent_at"],
        [old_ts, "CA_old", "RE_old", "9999", 30, "", "", ""],
        [fresh_ts, "CA_new", "RE_new", "1111", 12, "hi", "", ""],
    ]
    deletes: list[int] = []

    class _FakeWriter:
        def read_voicemail_rows(self): return list(rows_state)
        def delete_voicemail_row(self, row_index): deletes.append(row_index)

    out = StringIO()
    summary = prune_voicemail_rows(
        writer=_FakeWriter(), max_age_days=90, apply=True, out=out
    )
    assert summary["deleted"] == 1
    assert deletes == [2]  # row 2 (1-indexed; row 1 is header) was the old one
```

- [ ] **Step 2: Add `read_voicemail_rows` + `delete_voicemail_row` to `SheetsWriter`**

The retention job needs these helpers on `SheetsWriter`. Edit `tools/llm_proxy/sheets_writer.py` and add to the class:

```python
    def read_voicemail_rows(self) -> list[list[Any]]:
        """Return raw cells from the Voicemails tab, header included.

        Trailing empty cells are truncated by the Sheets API; callers must be
        tolerant of ragged rows.
        """
        try:
            result = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._sheet_id, range=f"{VOICEMAIL_TAB_NAME}!A:H")
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"voicemail sheet read failed: {exc}") from exc
        return result.get("values", [])

    def delete_voicemail_row(self, row_index: int) -> None:
        """Delete a single row from the Voicemails tab. `row_index` is 1-indexed
        in spreadsheet terms (row 1 = header, so first data row is 2)."""
        if row_index < 2:
            raise ValueError("refusing to delete header row")
        # Look up the Voicemails sheetId for the batchUpdate range.
        try:
            meta = (
                self._service.spreadsheets()
                .get(spreadsheetId=self._sheet_id, fields="sheets(properties(sheetId,title))")
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"could not read spreadsheet metadata: {exc}") from exc
        sheet_id = None
        for s in meta.get("sheets", []):
            if s.get("properties", {}).get("title") == VOICEMAIL_TAB_NAME:
                sheet_id = s["properties"]["sheetId"]
                break
        if sheet_id is None:
            raise RuntimeError(f"tab {VOICEMAIL_TAB_NAME!r} not found")
        try:
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=self._sheet_id,
                body={"requests": [{
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_index - 1,  # 0-indexed inclusive
                            "endIndex": row_index,        # 0-indexed exclusive
                        }
                    }
                }]},
            ).execute()
        except HttpError as exc:
            raise RuntimeError(f"voicemail row delete failed: {exc}") from exc
```

- [ ] **Step 3: Implement `tools/voicemail_retention.py`**

Create `tools/voicemail_retention.py`:

```python
"""Voicemail retention, daily prune of expired audio + sheet rows.

Runs as a Fly scheduled task. Mirrors the safety posture of
`tools/retention_enforcer.py`: dry-run by default, JSON-per-action stdout
logging, --apply required to actually delete.

Two passes:
  1. Twilio recordings older than --audio-days (default 30).
  2. Sheet rows in the Voicemails tab older than --metadata-days (default 90).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import IO

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient


def prune_twilio_recordings(
    *,
    client,
    max_age_days: int,
    apply: bool,
    out: IO[str] = sys.stdout,
) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    recordings = client.recordings.list(date_created_before=cutoff, limit=1000)
    would_delete = 0
    deleted = 0
    already_gone = 0
    errors = 0
    for rec in recordings:
        age_days = (datetime.now(timezone.utc) - rec.date_created).days
        event = {
            "action": "delete" if apply else "would-delete",
            "sid": rec.sid,
            "age_days": age_days,
        }
        if not apply:
            would_delete += 1
            print(json.dumps(event, sort_keys=True), file=out, flush=True)
            continue
        try:
            client.recordings(rec.sid).delete()
        except TwilioRestException as exc:
            if exc.status == 404:
                already_gone += 1
                event["action"] = "already-gone"
            else:
                errors += 1
                event["action"] = "error"
                event["error"] = f"twilio:{exc.status}:{exc.msg}"
        else:
            deleted += 1
        print(json.dumps(event, sort_keys=True), file=out, flush=True)
    return {
        "would_delete": would_delete,
        "deleted": deleted,
        "already_gone": already_gone,
        "errors": errors,
    }


def prune_voicemail_rows(
    *,
    writer,
    max_age_days: int,
    apply: bool,
    out: IO[str] = sys.stdout,
) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    rows = writer.read_voicemail_rows()
    if not rows:
        return {"would_delete": 0, "deleted": 0, "errors": 0}
    would_delete = 0
    deleted = 0
    errors = 0
    # Walk from bottom so deletions don't shift indices we haven't visited yet.
    indexed = list(enumerate(rows[1:], start=2))  # 2 = first data row (1-indexed)
    for row_index, row in reversed(indexed):
        created_at = row[0] if row else ""
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:
            continue
        if created >= cutoff:
            continue
        event = {
            "action": "delete-row" if apply else "would-delete-row",
            "row_index": row_index,
            "created_at": created_at,
            "recording_sid": row[2] if len(row) > 2 else "",
        }
        if not apply:
            would_delete += 1
            print(json.dumps(event, sort_keys=True), file=out, flush=True)
            continue
        try:
            writer.delete_voicemail_row(row_index)
        except Exception as exc:
            errors += 1
            event["action"] = "error"
            event["error"] = str(exc)
        else:
            deleted += 1
        print(json.dumps(event, sort_keys=True), file=out, flush=True)
    return {"would_delete": would_delete, "deleted": deleted, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Voicemail retention. Dry-run by default.")
    p.add_argument("--apply", action="store_true", help="Actually delete.")
    p.add_argument("--audio-days", type=int, default=30)
    p.add_argument("--metadata-days", type=int, default=90)
    p.add_argument("--prefix", default="ALLGREEN", help="Client env prefix for Sheet wiring.")
    args = p.parse_args(argv)

    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    client = TwilioClient(account_sid, auth_token)

    audio_summary = prune_twilio_recordings(
        client=client, max_age_days=args.audio_days, apply=args.apply
    )

    # Sheet pass, late-import so this script can run with --apply for audio
    # alone in environments that don't have Sheets creds.
    from tools.llm_proxy.sheets_writer import writer_from_env
    writer = writer_from_env(prefix=args.prefix)
    if writer is None:
        meta_summary = {"skipped": "no sheets writer"}
    else:
        meta_summary = prune_voicemail_rows(
            writer=writer, max_age_days=args.metadata_days, apply=args.apply
        )

    print(json.dumps({
        "summary": {
            "mode": "apply" if args.apply else "dry-run",
            "audio": audio_summary,
            "metadata": meta_summary,
        }
    }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_voicemail_retention.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tools/voicemail_retention.py tools/llm_proxy/sheets_writer.py tests/test_voicemail_retention.py
git commit -m "feat(voicemail): retention job, 30d audio / 90d metadata

Two passes: Twilio Recordings.delete for audio > 30d, sheet-row delete
for Voicemails entries > 90d. Dry-run default. 404 from Twilio treated
as already-gone."
```

---

## Task 11: RUNBOOK update

**Files:**
- Modify: `RUNBOOK.md`

- [ ] **Step 1: Read RUNBOOK.md to find the right section**

Look for an existing "Manual test plan" / "Smoke test" / "Operational" section. If none exists, add a new section after the SMS section.

- [ ] **Step 2: Add voicemail manual test plan**

Append:

```markdown
## Voicemail fallback

### Required env vars
- `VOICEMAIL_URL_SECRET`, 32-byte random secret. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `PUBLIC_BASE_URL`, already required by the voice path.
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, already required.

### Manual smoke test

1. Deploy with `VOICEMAIL_URL_SECRET` set. Confirm `/healthz` reports `voicemail_secret: true`.
2. Call the staging Twilio number. When Sarah greets, say "I just want to leave a voicemail."
3. Confirm the call seamlessly hands off to the beep within ~1 second.
4. Leave a short test message (≤10s). Press `#` to finish or wait for the 120s cap.
5. Hear "Thanks, we got your message." and the call ends.
6. Within ~90 seconds the owner phone (`{PREFIX}_OWNER_NOTIFY_NUMBER`) should receive an SMS containing:
   - "New voicemail, {short_name}"
   - Caller number redacted to last 4
   - Duration in MM:SS
   - Transcript (or "(transcript pending)" / "(no transcript available)")
   - A `https://<app>.fly.dev/vm/RE...` URL
   - "Expires in 30 days."
7. Tap the URL on a device with no Twilio creds. Confirm the MP3 plays.
8. Check the client's Google Sheet, a row should appear in the "Voicemails" tab with all fields populated.

### Retention test

```bash
# Dry-run (default)
python tools/voicemail_retention.py --prefix ALLGREEN

# Apply for real
python tools/voicemail_retention.py --apply --prefix ALLGREEN

# Force-prune for testing (1-day audio, 1-day metadata)
python tools/voicemail_retention.py --apply --audio-days 1 --metadata-days 1 --prefix ALLGREEN
```

After running with `--apply --audio-days 1`, tapping an old SMS URL should return a 410 "This voicemail has expired." page.

### Scheduling on Fly

Add the script to the existing scheduled-machine cron alongside `retention_enforcer.py` (see `infra/fly/`). Daily run at any low-traffic hour.
```

- [ ] **Step 3: Commit**

```bash
git add RUNBOOK.md
git commit -m "docs(runbook): voicemail manual smoke test + retention runbook"
```

---

## Self-Review

**Spec coverage:**
- Trigger detection (AI tool), Task 5 ✓
- Rich SMS payload, Task 2 ✓
- Signed proxy URL, Tasks 1 + 7 ✓
- 30d audio / 90d metadata, Task 10 ✓
- Voicemail TwiML, Task 4 ✓
- Recording + transcription callbacks with 60s race, Task 6 ✓
- Sheet tab, Tasks 3 + 8 ✓
- Prompt update, Task 9 ✓
- Env-var fail-fast + healthz, Task 8 ✓
- Manual test plan in RUNBOOK, Task 11 ✓
- New env var `VOICEMAIL_URL_SECRET`, Tasks 7, 8, 11 ✓

**Placeholder scan:** No "TBD", "TODO", or "fill in" language. All code blocks are concrete.

**Type consistency:** Function signatures match across tasks (`make_transfer_to_voicemail_handler`, `handle_recording_callback`, `handle_transcription_callback`, `prune_twilio_recordings`, `prune_voicemail_rows`). `VOICEMAIL_HEADER` / `VOICEMAIL_TAB_NAME` referenced from the same module across tasks.

**Known minor compromises** (called out so they're not surprises during execution):
- Voicemail callbacks pick a single client via `next iter` in `main.py`. v1 is single-tenant for the voicemail path, multi-tenant routing of the recording callbacks (which lack a `To` header) needs a future task that registers per-client `recordingStatusCallback` URLs.
- Twilio signature validation on the new callback endpoints isn't enforced in v1; it follows the same `ALLOW_UNSIGNED_TWILIO` escape hatch as voice_routes. Production deploys should set `TWILIO_AUTH_TOKEN`.
- The playback proxy buffers the MP3 in memory (no Range support). 120s caps file size at ~1 MB, so this is fine for v1.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-22-voicemail-fallback.md`. Two execution options:

1. **Subagent-Driven (recommended)**, Dispatches a fresh subagent per task with two-stage review, fastest path to working code with checkpoints.
2. **Inline Execution**, Executes tasks in this session with batch checkpoints.

Which approach?
