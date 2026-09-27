"""Tests for the voicemail subsystem: TwiML, callbacks, playback proxy, and tool."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.llm_proxy.voicemail import router as voicemail_router


@pytest.fixture(autouse=True)
def _isolate_twilio_auth_token(monkeypatch):
    """Make signature-validation tests hermetic regardless of run order.

    Importing tools.llm_proxy.main anywhere runs load_dotenv() at module load,
    which leaks the real .env TWILIO_AUTH_TOKEN into os.environ for the rest of
    the pytest session (load_dotenv mutates os.environ directly, not via
    monkeypatch). That made the ALLOW_UNSIGNED_TWILIO tests here pass in
    isolation but 403 in the full suite. Clear it up front; tests that need a
    token set it explicitly in their own body (which runs after this fixture).
    """
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(voicemail_router)
    return app


def test_voicemail_twiml_returns_record_verb() -> None:
    client = TestClient(_build_app())
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


def test_voicemail_twiml_embeds_client_id_in_callback_urls() -> None:
    """If the inbound /twilio/voicemail.xml URL carries ?client_id=X, both
    callback URLs (recording_status_callback, transcribe_callback) must
    propagate that client_id. Without this, the recording/transcription
    callbacks have no per-tenant routing key and silently attribute the
    voicemail to the first live client (data-integrity bug at 2+ clients).
    """
    client = TestClient(_build_app())
    with patch.dict(
        os.environ,
        {"ALLOW_UNSIGNED_TWILIO": "1", "PUBLIC_BASE_URL": "https://example.fly.dev"},
    ):
        resp = client.post(
            "/twilio/voicemail.xml?client_id=clearslope",
            data={"CallSid": "CA1", "To": "+15551112222"},
        )
    assert resp.status_code == 200
    body = resp.text
    assert "client_id=clearslope" in body
    # Both callback URLs must carry the routing key.
    assert body.count("client_id=clearslope") == 2


@pytest.mark.asyncio
async def test_transfer_to_voicemail_url_carries_client_id() -> None:
    """make_transfer_to_voicemail_handler with client_id=X must produce a
    redirect URL pointing at /twilio/voicemail.xml?client_id=X so the
    downstream callbacks route to the right tenant."""
    from tools.llm_proxy.voicemail import (
        _default_voicemail_url,
    )

    os.environ["PUBLIC_BASE_URL"] = "https://example.fly.dev"
    url = _default_voicemail_url(client_id="clearslope")
    assert url == "https://example.fly.dev/twilio/voicemail.xml?client_id=clearslope"

    # And with no client_id, no query string (back-compat).
    bare = _default_voicemail_url()
    assert bare == "https://example.fly.dev/twilio/voicemail.xml"


@pytest.mark.asyncio
async def test_voicemail_recording_dispatches_by_client_id() -> None:
    """voicemail_recording must look up the correct ClientContext via the
    ?client_id query and use ITS sheets_writer, not the hardwired voicemail
    defaults. Otherwise a second tenant's voicemails silently land in the
    first tenant's Sheet."""
    from fastapi import FastAPI

    from tools.llm_proxy.voicemail import router as voicemail_router

    rows_alpha: list[list] = []
    rows_beta: list[list] = []

    class _FakeWriter:
        def __init__(self, sink: list[list]) -> None:
            self._sink = sink
        def ensure_tab(self, *a, **kw): pass
        def append_to_tab(self, _tab, row): self._sink.append(row)
        def update_voicemail_transcript(self, *a, **kw): pass

    class _StubCfg:
        def __init__(self, cid: str) -> None:
            self.client_id = cid

    class _StubCtx:
        def __init__(self, cid: str, sink: list[list]) -> None:
            self.config = _StubCfg(cid)
            self.sheets_writer = _FakeWriter(sink)
            self.sms_notifier = None

    app = FastAPI()
    app.include_router(voicemail_router)
    app.state.clients = {
        "+1AAA": _StubCtx("alpha", rows_alpha),
        "+1BBB": _StubCtx("beta", rows_beta),
    }
    app.state.voicemail_pending = {}
    app.state.voicemail_transcripts = {}
    app.state.voicemail_pending_started_at = {}
    # Hardwired defaults must NOT be used when client_id is provided.
    app.state.voicemail_sheets_writer = None
    app.state.voicemail_sms_notifier = None
    app.state.voicemail_playback_url_builder = lambda sid: f"https://x/vm/{sid}"

    with patch.dict(os.environ, {"ALLOW_UNSIGNED_TWILIO": "1"}):
        client = TestClient(app)
        resp = client.post(
            "/twilio/voicemail/recording?client_id=beta",
            data={"CallSid": "CA1", "RecordingSid": "RE1",
                  "RecordingDuration": "9", "From": "+15551234"},
        )
    assert resp.status_code == 204
    assert len(rows_beta) == 1
    assert len(rows_alpha) == 0


def test_voicemail_twiml_rejects_bad_signature(monkeypatch) -> None:
    """POST to /twilio/voicemail.xml without a valid X-Twilio-Signature
    (and without the ALLOW_UNSIGNED_TWILIO escape hatch) must be rejected 403.

    Without this check, any HTTP client that discovers the URL can trigger TwiML
    generation; the recording/transcription callbacks did validate, leaving this
    endpoint as the only unprotected gap.
    """
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "real-token")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.fly.dev")
    monkeypatch.delenv("ALLOW_UNSIGNED_TWILIO", raising=False)
    client = TestClient(_build_app())
    resp = client.post(
        "/twilio/voicemail.xml",
        data={"CallSid": "CA1", "To": "+15551112222"},
    )
    assert resp.status_code == 403


def test_voicemail_recording_rejects_bad_signature(monkeypatch) -> None:
    """POST to /twilio/voicemail/recording without a valid X-Twilio-Signature
    (and without the ALLOW_UNSIGNED_TWILIO escape hatch) must be rejected 403."""
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "real-token")
    monkeypatch.delenv("ALLOW_UNSIGNED_TWILIO", raising=False)
    client = TestClient(_build_app())
    resp = client.post(
        "/twilio/voicemail/recording",
        data={"CallSid": "CA1", "RecordingSid": "RE1",
              "RecordingDuration": "5", "From": "+15551234567"},
    )
    assert resp.status_code == 403


def test_voicemail_transcription_rejects_bad_signature(monkeypatch) -> None:
    """POST to /twilio/voicemail/transcription without a valid X-Twilio-Signature
    (and without the ALLOW_UNSIGNED_TWILIO escape hatch) must be rejected 403."""
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "real-token")
    monkeypatch.delenv("ALLOW_UNSIGNED_TWILIO", raising=False)
    client = TestClient(_build_app())
    resp = client.post(
        "/twilio/voicemail/transcription",
        data={"RecordingSid": "RE1", "TranscriptionText": "hello"},
    )
    assert resp.status_code == 403


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
    assert end_call_called == []
    assert params.results[-1]["status"] == "error"
    assert "voicemail unavailable" in params.results[-1]["message"]


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
        wait_timeout_seconds=0.05,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row[1] == "CA1"
    assert row[2] == "RE1"
    assert row[3] == "0123"
    assert row[4] == 42
    assert row[5] == ""

    import asyncio as _aio
    await _aio.sleep(0.15)
    assert len(sms_sent) == 1
    assert sms_sent[0]["caller_number"] == "+16045550123"
    assert sms_sent[0]["duration_seconds"] == 42
    assert sms_sent[0]["transcript"] == "(transcript pending)"
    assert sms_sent[0]["playback_url"] == "https://x/vm/RE1?sig=s"


@pytest.mark.asyncio
async def test_recording_callback_prunes_stale_pending_entries() -> None:
    """A zombie pending entry (an _await_and_send task that crashed before its
    finally pop) must be swept by the next recording callback so the dict can't
    grow without bound at sustained voicemail volume.
    """
    import asyncio
    import time

    from tools.llm_proxy.voicemail import handle_recording_callback

    class _FakeSheetsWriter:
        def ensure_tab(self, tab, header): pass
        def append_to_tab(self, tab, row): pass
        def update_voicemail_transcript(self, sid, text): pass

    pending = {"RE_zombie": asyncio.Event()}
    transcripts = {"RE_zombie": "leftover text"}
    pending_started_at = {"RE_zombie": time.monotonic() - 600.0}  # 10 minutes old

    await handle_recording_callback(
        form={"CallSid": "CA1", "RecordingSid": "RE_new",
              "RecordingDuration": "5", "From": "+15551234"},
        sheets_writer=_FakeSheetsWriter(),
        sms_notifier=None,
        playback_url_builder=lambda sid: f"https://x/vm/{sid}",
        pending=pending,
        transcripts=transcripts,
        pending_started_at=pending_started_at,
        wait_timeout_seconds=0.01,
    )
    # Zombie entry pruned from all three dicts.
    assert "RE_zombie" not in pending
    assert "RE_zombie" not in transcripts
    assert "RE_zombie" not in pending_started_at
    # New entry registered with a timestamp.
    assert "RE_new" in pending
    assert "RE_new" in pending_started_at
    assert isinstance(pending_started_at["RE_new"], float)
    # Drain the background task so it doesn't bleed into other tests.
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_voicemail_row_expires_at_aligns_with_audio_retention() -> None:
    """The Sheet's playback_token_expires_at must reflect the lifetime of the
    underlying audio. Otherwise the owner sees a "valid for 30 days" timestamp
    on a link that returns 410 Gone after ~24 h (audio purged by retention job).
    """
    from datetime import datetime, timedelta, timezone

    from tools.llm_proxy.voicemail import (
        PLAYBACK_TOKEN_EXPIRY_DAYS,
        handle_recording_callback,
    )

    # Sanity check: the constant must match the voicemail_retention --audio-days
    # default (1) so the Sheet timestamp never overpromises.
    assert PLAYBACK_TOKEN_EXPIRY_DAYS == 1

    rows: list[list] = []

    class _FakeSheetsWriter:
        def ensure_tab(self, tab, header): pass
        def append_to_tab(self, tab, row): rows.append(row)
        def update_voicemail_transcript(self, sid, text): pass

    t_in = datetime.now(timezone.utc)
    await handle_recording_callback(
        form={"CallSid": "CA1", "RecordingSid": "RE1",
              "RecordingDuration": "5", "From": "+15551234"},
        sheets_writer=_FakeSheetsWriter(),
        sms_notifier=None,
        playback_url_builder=lambda sid: f"https://x/vm/{sid}",
        pending={},
        transcripts={},
        wait_timeout_seconds=0.01,
    )

    assert len(rows) == 1
    expires_at_str = rows[0][6]
    # ISO 8601 with trailing Z, switch to +00:00 for fromisoformat.
    expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
    delta = expires_at - t_in
    # ~1 day from creation, with a generous window for test scheduler jitter.
    assert timedelta(hours=23) < delta < timedelta(hours=25), (
        f"expires_at delta {delta} not ~1 day (was {expires_at_str})"
    )


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

    await handle_recording_callback(
        form={"CallSid": "CA1", "RecordingSid": "RE1",
              "RecordingDuration": "12", "From": "+16045550999"},
        sheets_writer=_FakeSheetsWriter(),
        sms_notifier=_FakeNotifier(),
        playback_url_builder=lambda sid: f"https://x/vm/{sid}",
        pending=pending,
        transcripts=transcripts,
        wait_timeout_seconds=5.0,
    )

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
    await _aio.sleep(0.15)
    assert len(sms_sent) == 1
    assert sms_sent[0]["transcript"] == "(transcript pending)"

    await handle_transcription_callback(
        form={"RecordingSid": "RE1", "TranscriptionText": "Late arrival text."},
        pending=pending,
        transcripts=transcripts,
        sheets_writer=_FakeSheetsWriter(),
    )
    assert len(sms_sent) == 1


@pytest.mark.asyncio
async def test_two_concurrent_voicemails_have_independent_state() -> None:
    """Two voicemails in flight at once should not cross-contaminate."""
    from tools.llm_proxy.voicemail import handle_recording_callback, handle_transcription_callback

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

    # Start two voicemails with different SIDs
    await handle_recording_callback(
        form={"CallSid": "CA1", "RecordingSid": "RE1",
              "RecordingDuration": "10", "From": "+15550000001"},
        sheets_writer=_FakeSheetsWriter(), sms_notifier=_FakeNotifier(),
        playback_url_builder=lambda sid: f"https://x/vm/{sid}",
        pending=pending, transcripts=transcripts, wait_timeout_seconds=5.0,
    )
    await handle_recording_callback(
        form={"CallSid": "CA2", "RecordingSid": "RE2",
              "RecordingDuration": "20", "From": "+15550000002"},
        sheets_writer=_FakeSheetsWriter(), sms_notifier=_FakeNotifier(),
        playback_url_builder=lambda sid: f"https://x/vm/{sid}",
        pending=pending, transcripts=transcripts, wait_timeout_seconds=5.0,
    )

    # Transcription for RE2 arrives first
    await handle_transcription_callback(
        form={"RecordingSid": "RE2", "TranscriptionText": "second voicemail"},
        pending=pending, transcripts=transcripts, sheets_writer=_FakeSheetsWriter(),
    )
    # Then for RE1
    await handle_transcription_callback(
        form={"RecordingSid": "RE1", "TranscriptionText": "first voicemail"},
        pending=pending, transcripts=transcripts, sheets_writer=_FakeSheetsWriter(),
    )

    import asyncio as _aio
    await _aio.sleep(0.05)
    assert len(sms_sent) == 2
    by_caller = {s["caller_number"]: s for s in sms_sent}
    assert by_caller["+15550000001"]["transcript"] == "first voicemail"
    assert by_caller["+15550000001"]["duration_seconds"] == 10
    assert by_caller["+15550000002"]["transcript"] == "second voicemail"
    assert by_caller["+15550000002"]["duration_seconds"] == 20
    # Both pending entries cleaned up
    assert "RE1" not in pending
    assert "RE2" not in pending


class _FakeStreamResponse:
    """Mimics an httpx streaming response: yields the body in chunks via
    aiter_bytes() and refuses to be buffered via .content (S14)."""

    def __init__(self, status_code: int, chunks: list[bytes], headers: dict):
        self.status_code = status_code
        self._chunks = chunks
        self.headers = headers

    async def aiter_bytes(self, chunk_size: int | None = None):
        for chunk in self._chunks:
            yield chunk

    async def aread(self) -> bytes:
        return b"".join(self._chunks)

    async def aclose(self) -> None:
        return None

    @property
    def content(self) -> bytes:  # pragma: no cover - guard, must never be hit
        raise AssertionError("playback must stream, not buffer the whole recording (.content)")


def _patch_streaming_upstream(monkeypatch, *, status_code: int, chunks: list[bytes], headers: dict) -> dict:
    """Patch httpx so the upstream Twilio fetch is a streaming send; record how
    it was called. .get is poisoned so a buffering implementation fails loudly."""
    sent: dict = {}

    async def _fake_send(self, request, *, auth=None, stream=False, **kwargs):
        assert stream is True, "playback must open the upstream with stream=True"
        sent["url"] = str(request.url)
        sent["auth"] = auth
        sent["stream"] = stream
        return _FakeStreamResponse(status_code, chunks, headers)

    async def _no_get(self, *args, **kwargs):
        raise AssertionError("playback must stream via .send(stream=True), not buffer via .get")

    monkeypatch.setattr("httpx.AsyncClient.send", _fake_send)
    monkeypatch.setattr("httpx.AsyncClient.get", _no_get)
    return sent


def test_playback_proxy_streams_audio_without_buffering(monkeypatch) -> None:
    """The recording must be streamed through in chunks (S14), never read whole
    into memory, multiple concurrent playbacks would otherwise spike RAM on the
    1 GB box. Delivered in multiple chunks; .content/.get are poisoned."""
    from tools.llm_proxy.voicemail_urls import sign_playback_url

    SECRET = b"x" * 32
    monkeypatch.setenv("VOICEMAIL_URL_SECRET", SECRET.decode("latin-1"))
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACfake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")

    sent = _patch_streaming_upstream(
        monkeypatch,
        status_code=200,
        chunks=[b"FAKE-", b"MP3-", b"BYTES"],
        headers={"Content-Type": "audio/mpeg"},
    )

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
    assert resp.content == b"FAKE-MP3-BYTES"  # chunks reassembled by the client
    assert resp.headers["content-type"] == "audio/mpeg"
    assert sent.get("stream") is True
    assert "REabc" in sent["url"]
    assert sent["auth"] == ("ACfake", "tok")


def test_playback_proxy_closes_client_when_upstream_send_fails(monkeypatch) -> None:
    """If the upstream fetch raises (timeout/network), the httpx client must
    still be closed. The finally that closes it lives in the streaming
    generator, which is never created when send() itself raises."""
    from tools.llm_proxy.voicemail_urls import sign_playback_url
    import httpx
    import time as _time

    SECRET = b"z" * 32
    monkeypatch.setenv("VOICEMAIL_URL_SECRET", SECRET.decode("latin-1"))
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")

    closed = {"n": 0}

    async def _boom_send(self, *args, **kwargs):
        raise httpx.ConnectTimeout("upstream down")

    async def _record_aclose(self):
        closed["n"] += 1

    monkeypatch.setattr("httpx.AsyncClient.send", _boom_send)
    monkeypatch.setattr("httpx.AsyncClient.aclose", _record_aclose)

    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)
    url = sign_playback_url(
        base_url="http://testserver",
        recording_sid="REx",
        expires_epoch=int(_time.time()) + 3600,
        secret=SECRET,
    )
    resp = client.get(url.split("testserver", 1)[1])
    assert resp.status_code >= 500
    assert closed["n"] >= 1, "AsyncClient was not closed when send() raised"


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

    _patch_streaming_upstream(monkeypatch, status_code=404, chunks=[b""], headers={})

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
