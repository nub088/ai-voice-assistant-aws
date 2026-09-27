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

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Hangup, Record, VoiceResponse

from .sheets_writer import VOICEMAIL_HEADER, VOICEMAIL_TAB_NAME
from .twilio_auth import signed_webhook_url, unsigned_twilio_allowed
from .voicemail_urls import InvalidToken, verify_playback_token

logger = logging.getLogger("voicemail")

router = APIRouter()

# Hard cap matches Twilio's built-in transcription limit (2 min).
RECORDING_MAX_LENGTH_SECONDS = 120

# Strong refs to in-flight background tasks so they aren't GC'd mid-flight.
_VOICEMAIL_BG_TASKS: set[asyncio.Task] = set()


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


def _validate_twilio_signature(request: Request, form: dict[str, str]) -> None:
    """Validate Twilio's HMAC-SHA1 request signature on inbound callbacks.

    Mirrors `voice_routes._validate_twilio_signature` but reads the account
    auth_token from env (these callbacks are not per-client routed). Honors
    the same `ALLOW_UNSIGNED_TWILIO` dev escape hatch.
    """
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not auth_token:
        if unsigned_twilio_allowed():
            logger.warning(
                "TWILIO_AUTH_TOKEN empty and ALLOW_UNSIGNED_TWILIO=1, "
                "skipping voicemail signature validation (dev only)"
            )
            return
        logger.error("TWILIO_AUTH_TOKEN required in production, rejecting voicemail callback")
        raise HTTPException(status_code=500, detail="server misconfigured: signature verification unavailable")
    signature = request.headers.get("x-twilio-signature", "")
    url = signed_webhook_url(request)
    validator = RequestValidator(auth_token)
    if not validator.validate(url, form, signature):
        logger.warning("voicemail twilio signature validation failed for url=%s", url)
        raise HTTPException(status_code=403, detail="invalid twilio signature")


@router.post("/twilio/voicemail.xml")
async def voicemail_twiml(request: Request) -> Response:
    """Return TwiML that records the caller's message with transcription.

    If the inbound URL carries ?client_id=X, that key is propagated into the
    recording_status_callback and transcribe_callback URLs so the downstream
    callbacks can dispatch to the correct tenant's sheets_writer/sms_notifier.
    """
    form = dict(await request.form())
    _validate_twilio_signature(request, form)
    base = _public_base_url(request)
    client_id = (request.query_params.get("client_id") or "").strip()
    if client_id:
        from urllib.parse import urlencode
        query = "?" + urlencode({"client_id": client_id})
    else:
        query = ""
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
            transcribe_callback=f"{base}/twilio/voicemail/transcription{query}",
            recording_status_callback=f"{base}/twilio/voicemail/recording{query}",
            finish_on_key="#",
        )
    )
    twiml.say("Thanks, we got your message. Goodbye.")
    twiml.append(Hangup())
    return Response(content=str(twiml), media_type="application/xml")


def _default_twilio_rest_client() -> TwilioClient:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")
    return TwilioClient(account_sid, auth_token)


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


def _default_voicemail_url(client_id: str = "") -> str:
    """Build the public /twilio/voicemail.xml URL Twilio is redirected to.

    When `client_id` is set, append it as a query param so the downstream
    recording/transcription callbacks can route to the correct tenant's
    sheets_writer and sms_notifier rather than falling through to a
    single hardwired voicemail-handling client.
    """
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
    url = f"https://{host}/twilio/voicemail.xml"
    if client_id:
        from urllib.parse import urlencode
        url += "?" + urlencode({"client_id": client_id})
    return url


def make_transfer_to_voicemail_handler(
    *,
    call_sid: str,
    on_end_call: Callable[[], Awaitable[None]],
    twilio_client_factory: Callable[[], TwilioClient] = _default_twilio_rest_client,
    voicemail_url_fn: Callable[[], str] | None = None,
    client_id: str = "",
) -> Callable[[Any], Awaitable[None]]:
    """Build a handler that redirects the live Twilio call into voicemail TwiML.

    On `calls.update` success: tells the model "transferring" and triggers
    end-of-pipeline so the Pipecat session winds down cleanly (Twilio kills
    the Stream WS as soon as the call moves).

    On failure: reports error to the model and does NOT end the call, so the
    AI can fall back to taking the caller's info via `log_request`.
    """
    # Default URL builder embeds client_id so the recording/transcription
    # callbacks can dispatch back to this client's writer/notifier.
    _url_fn: Callable[[], str] = (
        voicemail_url_fn if voicemail_url_fn is not None
        else (lambda: _default_voicemail_url(client_id=client_id))
    )

    async def handler(params: Any) -> None:
        args = params.arguments or {}
        reason = (args.get("reason") or "").strip() or "caller requested voicemail"
        try:
            url = _url_fn()
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


VOICEMAIL_PENDING_TRANSCRIPT = "(transcript pending)"

# How long the signed playback URL (and the Sheet's `playback_token_expires_at`
# column) remains valid. Aligned with the audio-retention default in
# `tools/voicemail_retention.py` (--audio-days 1): once the audio is purged,
# the link returns 410, so a longer-lived token would just create a
# working-looking link that 410s in the background. Keep these in sync.
PLAYBACK_TOKEN_EXPIRY_DAYS = 1

# Maximum lifetime of an entry in the voicemail_pending dict. Twilio's
# transcription callback nominally arrives within ~60 s; entries older than
# 5× that bound are zombies whose _await_and_send task either crashed
# pre-finally or was lost. Aggressively conservative to avoid touching
# genuinely in-flight handlers.
PENDING_VOICEMAIL_TTL_SECONDS = 300.0


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
    pending_started_at: dict | None = None,
    wait_timeout_seconds: float = 60.0,
) -> None:
    """Process Twilio's recording-status callback.

    Appends an immediate Voicemails row (empty transcript) and spawns a
    background task that awaits the transcription callback (or times out
    after `wait_timeout_seconds`) before sending the owner SMS.

    If `pending_started_at` is provided, this also sweeps stale entries from
    `pending`, `transcripts`, and `pending_started_at` itself (entries older
    than PENDING_VOICEMAIL_TTL_SECONDS). Production code passes the sidecar
    timestamp dict from app.state; tests that don't care about TTL can omit it.
    """
    # Sweep zombie entries first so the dicts can't grow without bound.
    if pending_started_at is not None:
        import time as _time
        now = _time.monotonic()
        stale = [
            sid for sid, ts in pending_started_at.items()
            if now - ts > PENDING_VOICEMAIL_TTL_SECONDS
        ]
        for sid in stale:
            pending.pop(sid, None)
            transcripts.pop(sid, None)
            pending_started_at.pop(sid, None)
        if stale:
            logger.warning(
                "voicemail: pruned %d stale pending entries (>%ds old)",
                len(stale), int(PENDING_VOICEMAIL_TTL_SECONDS),
            )

    call_sid = form.get("CallSid", "")
    recording_sid = form.get("RecordingSid", "")
    duration = int(form.get("RecordingDuration") or 0)
    caller_number = form.get("From", "")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    expires_at = (
        (datetime.now(timezone.utc) + timedelta(days=PLAYBACK_TOKEN_EXPIRY_DAYS))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    if not recording_sid:
        logger.warning("recording callback missing RecordingSid: %r", form)
        return

    try:
        if sheets_writer is not None:
            # googleapiclient is sync, offload to a thread so the recording
            # callback doesn't stall the event loop for the Google round-trip.
            await asyncio.to_thread(
                sheets_writer.ensure_tab, VOICEMAIL_TAB_NAME, VOICEMAIL_HEADER
            )
            await asyncio.to_thread(
                sheets_writer.append_to_tab,
                VOICEMAIL_TAB_NAME,
                [
                    created_at,
                    call_sid,
                    recording_sid,
                    _last4_digits(caller_number),
                    duration,
                    "",
                    expires_at,
                    "",
                ],
            )
    except Exception:
        logger.exception("voicemail row append failed sid=%s", recording_sid)

    event = asyncio.Event()
    pending[recording_sid] = event
    if pending_started_at is not None:
        import time as _time
        pending_started_at[recording_sid] = _time.monotonic()

    async def _await_and_send():
        try:
            await asyncio.wait_for(event.wait(), timeout=wait_timeout_seconds)
            transcript_text = transcripts.pop(recording_sid, "") or VOICEMAIL_PENDING_TRANSCRIPT
        except asyncio.TimeoutError:
            transcript_text = VOICEMAIL_PENDING_TRANSCRIPT
        finally:
            pending.pop(recording_sid, None)
            if pending_started_at is not None:
                pending_started_at.pop(recording_sid, None)

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

    task = asyncio.create_task(_await_and_send())
    _VOICEMAIL_BG_TASKS.add(task)
    task.add_done_callback(_VOICEMAIL_BG_TASKS.discard)


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
                await asyncio.to_thread(
                    sheets_writer.update_voicemail_transcript, recording_sid, text
                )
        except Exception:
            logger.exception("late transcript update failed sid=%s", recording_sid)


def _resolve_voicemail_targets(
    request: Request,
) -> tuple[Any, Any]:
    """Return (sheets_writer, sms_notifier) for this voicemail callback.

    If ?client_id=X is present and matches a registered tenant, use that
    tenant's writer/notifier. Otherwise fall back to the hardwired
    app.state.voicemail_sheets_writer / voicemail_sms_notifier set at
    startup. The fallback preserves v1 behavior (one voicemail-handling
    client) and prevents 500s when a callback arrives without a client_id.
    """
    state = request.app.state
    client_id = (request.query_params.get("client_id") or "").strip()
    if client_id:
        clients = getattr(state, "clients", {}) or {}
        for ctx in clients.values():
            if getattr(getattr(ctx, "config", None), "client_id", None) == client_id:
                return ctx.sheets_writer, ctx.sms_notifier
        logger.warning(
            "voicemail callback: client_id=%r not found in registry, falling back to defaults",
            client_id,
        )
    return (
        getattr(state, "voicemail_sheets_writer", None),
        getattr(state, "voicemail_sms_notifier", None),
    )


@router.post("/twilio/voicemail/recording")
async def voicemail_recording(request: Request) -> Response:
    form = dict(await request.form())
    _validate_twilio_signature(request, form)
    state = request.app.state
    sheets_writer, sms_notifier = _resolve_voicemail_targets(request)
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
        pending_started_at=getattr(state, "voicemail_pending_started_at", None),
    )
    return Response(status_code=204)


@router.post("/twilio/voicemail/transcription")
async def voicemail_transcription(request: Request) -> Response:
    form = dict(await request.form())
    _validate_twilio_signature(request, form)
    state = request.app.state
    sheets_writer, _ = _resolve_voicemail_targets(request)
    await handle_transcription_callback(
        form=form,
        pending=state.voicemail_pending,
        transcripts=state.voicemail_transcripts,
        sheets_writer=sheets_writer,
    )
    return Response(status_code=204)


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
    # Stream the recording through in chunks rather than buffering the whole
    # MP3 into memory (S14): a 2-minute voicemail is ~1-2 MB and several
    # concurrent playbacks would otherwise spike RAM on the 1 GB box. The
    # client + upstream response are kept open for the lifetime of the stream
    # and closed in the generator's finally.
    client = httpx.AsyncClient(timeout=15.0)
    try:
        upstream = await client.send(
            client.build_request("GET", twilio_url), auth=(sid, tok), stream=True
        )
    except Exception:
        # send() failed (timeout/network/DNS) before the streaming generator
        # exists, so its finally can't run, close the client here.
        await client.aclose()
        raise
    if upstream.status_code == 404:
        await upstream.aclose()
        await client.aclose()
        return HTMLResponse(
            status_code=410,
            content=(
                "<!doctype html><meta charset=utf-8><title>Voicemail expired</title>"
                "<p>This voicemail has expired.</p>"
            ),
        )
    if upstream.status_code != 200:
        logger.warning("twilio recording fetch sid=%s status=%d", recording_sid, upstream.status_code)
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="upstream error")

    async def _stream_recording():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream_recording(),
        media_type=upstream.headers.get("Content-Type", "audio/mpeg"),
    )
