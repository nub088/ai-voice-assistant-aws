"""
FastAPI routes that connect Twilio Programmable Voice to the Pipecat pipeline.

Two endpoints:

  POST /twilio/voice, Twilio fetches this when a call comes in. Looks up
                         the client by the inbound `To` number (D-01), checks
                         minute ceiling (D-09), then returns TwiML that opens
                         a Media Stream WebSocket back to /twilio/stream.
                         Validates Twilio's request signature per client (D-12).

  WS /twilio/stream, Twilio opens this WebSocket and starts streaming
                         μ-law 8 kHz audio frames. Recovers the ClientContext
                         via the pending_calls bridge, then hands the socket to
                         `voice_pipeline.run_voice_session`.

ClientContext comes from `request.app.state.clients` (dict[twilio_number,
ClientContext]) built at startup by main.py.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from .twilio_auth import signed_webhook_url, unsigned_twilio_allowed
from .voice_pipeline import run_voice_session

logger = logging.getLogger("voice_routes")

router = APIRouter()


def _redact_phone(number: str) -> str:
    """Mask all but the last 4 digits of an E.164 number for log output."""
    if not number:
        return ""
    return f"***{number[-4:]}" if len(number) >= 4 else "***"


def _public_base_url(request: Request) -> str:
    """Return `wss://<host>` for the Stream URL.

    Prefers `PUBLIC_BASE_URL` env (set on fly.io to `<app>.fly.dev`).

    S11: the inbound Host / X-Forwarded-Host header is attacker-controlled, a
    poisoned value would point Twilio's Media Stream at an attacker-controlled
    host (call-audio MITM). So the header fallback is only honored as a local-dev
    convenience (ALLOW_UNSIGNED_TWILIO); in production PUBLIC_BASE_URL is required.
    """
    explicit = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        host = explicit.removeprefix("https://").removeprefix("http://").removeprefix("wss://").removeprefix("ws://")
        return f"wss://{host}"
    if unsigned_twilio_allowed():
        forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if forwarded_host:
            return f"wss://{forwarded_host}"
    raise HTTPException(
        status_code=500,
        detail="PUBLIC_BASE_URL must be set (refusing to trust the Host header)",
    )


# How long a pending_calls entry (a /twilio/voice POST awaiting its
# /twilio/stream WS) is allowed to live before it's swept. Twilio normally
# opens the WS within ~1 s; entries older than this represent calls that
# never produced a stream (network drop, spoofed POST, etc.).
PENDING_CALL_TTL_SECONDS = 30.0


def _prune_stale_pending_calls(
    pending: dict[str, str],
    started_at: dict[str, float],
    *,
    now: float,
    ttl: float = PENDING_CALL_TTL_SECONDS,
) -> int:
    """Remove entries older than `ttl` seconds. Returns number pruned.

    Called once on each /twilio/stream entry. Keeps the bridge dict bounded
    even under signature-bypass conditions where a spoofed POST could otherwise
    inflate it without limit.
    """
    stale = [sid for sid, ts in started_at.items() if now - ts > ttl]
    for sid in stale:
        pending.pop(sid, None)
        started_at.pop(sid, None)
    if stale:
        logger.warning(
            "twilio_stream: pruned %d stale pending_calls entries (>%ds old)",
            len(stale), int(ttl),
        )
    return len(stale)


# How long a signed stream routing token stays valid (S10). Twilio opens the
# Media Stream WS within ~1 s of the /twilio/voice POST; 5 min is generous slack.
STREAM_TOKEN_TTL_SECONDS = 300


def _routing_sig(to_number: str, exp: int, secret: str) -> str:
    """base64url HMAC-SHA256 over `<to_number>|<exp>`, keyed by the client's
    Twilio auth token. Proves a stream routing param was minted by our
    signature-validated /twilio/voice rather than forged by a direct WS caller."""
    msg = f"{to_number}|{exp}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _verify_routing_sig(
    to_number: str, exp_str: str, sig: str, secret: str, now: int
) -> bool:
    """Constant-time-verify a routing signature and check expiry."""
    if not exp_str or not sig:
        return False
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    if now > exp:
        return False
    return hmac.compare_digest(_routing_sig(to_number, exp, secret), sig)


def _resolve_stream_to_number(
    custom_params: dict,
    clients: dict,
    pending_calls: dict,
    call_sid: str,
    *,
    now: int,
) -> str | None:
    """Resolve the inbound `To` number for an incoming Media Stream (SC1 + S10).

    Prefers the signed TwiML Stream `to` parameter, Twilio echoes it back in
    the WS `start` event, so it is machine-independent (survives multi-machine
    and rolling deploys). The signature (`sig`+`exp`, keyed by the client's auth
    token) makes it unforgeable: a direct WS caller can't fabricate a `to` to
    start a paid session without the secret.

    Resolution order:
      1. Signed `to` param that verifies against the claimed client's secret.
      2. Dev/unsigned: client has no auth token (ALLOW_UNSIGNED) → trust `to`.
      3. Legacy same-machine bridge (`pending_calls`), deploy-window fallback.
    A forged/expired signed param is NOT trusted; it falls through to the bridge.
    """
    to = str(custom_params.get("to") or "")
    if to:
        ctx = clients.get(to)
        secret = ctx.auth_token if ctx is not None else ""
        if not secret:
            # No secret to verify against (dev / unknown number). Unknown
            # numbers are dropped by the caller's clients lookup anyway.
            return to
        if _verify_routing_sig(
            to, str(custom_params.get("exp") or ""), str(custom_params.get("sig") or ""), secret, now
        ):
            return to
        # Signed param present but invalid → do not trust it; try the bridge.
    return pending_calls.get(call_sid)


def _ceiling_exceeded(
    used: float,
    pending_calls: dict,
    active_calls: dict,
    to_number: str,
    client_id: str,
    ceiling: int | None,
) -> bool:
    """Admission check that counts in-flight calls, not just completed minutes (SC5).

    `used` (minute_counters) only updates at end-of-call, so N calls arriving in
    the same window all read the same stale value and are all admitted, blowing
    past the cap. We add the calls already in flight: awaiting-stream calls live
    in `pending_calls` (call_sid -> to_number) and streaming calls are counted in
    `active_calls` (client_id -> count). Each in-flight call charges >= 1 minute,
    so reserving 1 apiece bounds the overrun. Returns False when no ceiling is set.
    """
    if ceiling is None:
        return False
    pending = sum(1 for v in pending_calls.values() if v == to_number)
    active = active_calls.get(client_id, 0)
    return used + pending + active >= ceiling


def _validate_twilio_signature(
    request: Request, form: dict[str, str], auth_token: str
) -> None:
    """Validate Twilio's HMAC-SHA1 request signature using the client's auth_token (D-12)."""
    if not auth_token:
        if unsigned_twilio_allowed():
            logger.warning("auth_token empty and ALLOW_UNSIGNED_TWILIO=1, skipping signature validation (dev only)")
            return
        logger.error("auth_token is required in production, rejecting request")
        raise HTTPException(status_code=500, detail="server misconfigured: signature verification unavailable")
    signature = request.headers.get("x-twilio-signature", "")
    # Twilio signs the URL it called. With fly.io's HTTPS proxy in front, the
    # caller-facing URL is https://<host><path>, which is what `request.url`
    # reports when uvicorn runs with --proxy-headers --forwarded-allow-ips '*'.
    url = signed_webhook_url(request)
    validator = RequestValidator(auth_token)
    if not validator.validate(url, form, signature):
        logger.warning("twilio signature validation failed for url=%s", url)
        raise HTTPException(status_code=403, detail="invalid twilio signature")


@router.post("/twilio/voice")
async def twilio_voice(request: Request) -> Response:
    """Route inbound call to the correct ClientContext and return Media Stream TwiML.

    1. Extract `To` (routing key, D-01) and look up client in registry.
    2. Validate Twilio signature using client's auth_token (D-12).
    3. Enforce minute ceiling before streaming (D-09), over-limit calls get
       a rejection TTS + Hangup, never enter the pipeline.
    4. Store call_sid → to_number in pending_calls so /twilio/stream can
       recover the ClientContext.
    """
    t_in = time.perf_counter()
    form = dict(await request.form())

    to_number = form.get("To", "")
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")

    # Client lookup by inbound Twilio number (D-01)
    client_ctx = request.app.state.clients.get(to_number)
    if client_ctx is None:
        logger.warning(
            "twilio_voice call_sid=%s unknown To=%s, no client configured for this number",
            call_sid, _redact_phone(to_number),
        )
        raise HTTPException(status_code=404, detail=f"no client configured for {to_number}")

    _validate_twilio_signature(request, form, client_ctx.auth_token)

    # Ceiling check BEFORE returning streaming TwiML (D-09). Counts in-flight
    # reservations so a burst of concurrent calls can't all pass a stale check
    # and overrun the cap (D-09 / SC5).
    if client_ctx.config.minute_ceiling is not None:
        used = request.app.state.minute_counters.get(client_ctx.config.client_id, 0.0)
        if _ceiling_exceeded(
            used,
            request.app.state.pending_calls,
            request.app.state.active_calls,
            to_number,
            client_ctx.config.client_id,
            client_ctx.config.minute_ceiling,
        ):
            logger.warning(
                "twilio_voice call_sid=%s client_id=%s ceiling=%d used=%.0f inflight=%d "
                ", rejecting over-limit call",
                call_sid, client_ctx.config.client_id,
                client_ctx.config.minute_ceiling, used,
                sum(1 for v in request.app.state.pending_calls.values() if v == to_number)
                + request.app.state.active_calls.get(client_ctx.config.client_id, 0),
            )
            twiml = VoiceResponse()
            twiml.say(
                "We're sorry, our receptionist service is temporarily unavailable. "
                "Please call back later or send us an email. Thank you."
            )
            twiml.hangup()
            return Response(content=str(twiml), media_type="application/xml")

    logger.info(
        "twilio_voice call_sid=%s client_id=%s from=%s",
        call_sid, client_ctx.config.client_id, _redact_phone(from_number),
    )

    # Bridge: call_sid → to_number so /twilio/stream knows which client owns this call.
    # The sidecar pending_calls_started_at dict carries a monotonic timestamp so
    # /twilio/stream can prune entries whose WS never arrives.
    request.app.state.pending_calls[call_sid] = to_number
    request.app.state.pending_calls_started_at[call_sid] = time.monotonic()

    base = _public_base_url(request)
    twiml = VoiceResponse()
    connect = Connect()
    stream = Stream(url=f"{base}/twilio/stream")
    # SC1: carry the inbound To number on the Stream so /twilio/stream can
    # resolve the client without the in-process bridge, Twilio echoes it back
    # in the WS start event, machine-independent across deploys/multi-machine.
    # S10: HMAC-sign it (to+exp) with the client's auth token so a direct WS
    # caller can't forge a `to` to start a paid session. In dev (no auth token)
    # the param is sent unsigned.
    stream.parameter(name="to", value=to_number)
    if client_ctx.auth_token:
        exp = int(time.time()) + STREAM_TOKEN_TTL_SECONDS
        stream.parameter(name="exp", value=str(exp))
        stream.parameter(name="sig", value=_routing_sig(to_number, exp, client_ctx.auth_token))
    if from_number:
        stream.parameter(name="from", value=from_number)
    connect.append(stream)
    twiml.append(connect)
    logger.info(
        "twilio_voice_timing call_sid=%s total_ms=%.0f",
        call_sid, (time.perf_counter() - t_in) * 1000,
    )
    return Response(content=str(twiml), media_type="application/xml")


@router.websocket("/twilio/stream")
async def twilio_stream(websocket: WebSocket) -> None:
    """Read Twilio's `connected` + `start` control frames, recover ClientContext
    via pending_calls bridge, then hand the socket to the Pipecat pipeline.

    Timing markers (t_accept → t_ready) are logged once per call to localize
    the cold-path Media Stream WS failure mode (Twilio error 31921). See
    call_drops_diagnosis.txt at the project root.
    """
    t_entry = time.perf_counter()
    await websocket.accept()
    t_accept = time.perf_counter()
    try:
        # Twilio sends a `connected` event first, then a `start` event with
        # streamSid + callSid in its `start` block.
        first = await websocket.receive_text()
        t_connected = time.perf_counter()
        first_data = json.loads(first)
        if first_data.get("event") != "connected":
            logger.warning("expected 'connected' event, got %r, closing", first_data.get("event"))
            await websocket.close(code=1002)
            return

        second = await websocket.receive_text()
        t_start = time.perf_counter()
        second_data = json.loads(second)
        if second_data.get("event") != "start":
            logger.warning("expected 'start' event, got %r, closing", second_data.get("event"))
            await websocket.close(code=1002)
            return

        start = second_data.get("start") or {}
        stream_sid = str(start.get("streamSid") or "")
        call_sid = str(start.get("callSid") or "")
        if not stream_sid or not call_sid:
            logger.warning("missing streamSid/callSid in start event: %r", start)
            await websocket.close(code=1002)
            return
        # Twilio echoes <Parameter> tags from the TwiML <Stream> back here.
        custom_params = start.get("customParameters") or {}
        caller_number = str(custom_params.get("from") or "")
    except WebSocketDisconnect:
        logger.info(
            "twilio_stream: caller disconnected before start event "
            "(accept_ms=%.0f total_ms=%.0f)",
            (t_accept - t_entry) * 1000,
            (time.perf_counter() - t_entry) * 1000,
        )
        return
    except json.JSONDecodeError:
        logger.warning("twilio_stream: non-JSON control frame, closing")
        await websocket.close(code=1002)
        return

    state = websocket.app.state

    # Sweep stale pending_calls entries (Twilio POST but no WS within TTL) so
    # a misconfigured signature bypass can't inflate this dict unboundedly.
    _prune_stale_pending_calls(
        state.pending_calls, state.pending_calls_started_at, now=time.monotonic()
    )

    # Recover the inbound To number (D-01, D-03). SC1/S10: prefer the signed,
    # machine-independent Stream parameter; the pending_calls bridge is only a
    # same-machine deploy-window fallback. Forged params are rejected here.
    to_number = _resolve_stream_to_number(
        custom_params, state.clients, state.pending_calls, call_sid, now=int(time.time())
    )
    state.pending_calls.pop(call_sid, None)
    state.pending_calls_started_at.pop(call_sid, None)
    if to_number is None:
        logger.warning(
            "twilio_stream: no 'to' stream param and no pending_calls entry for "
            "call_sid=%s, dropping (bridge_keys=%d)",
            call_sid, len(state.pending_calls),
        )
        await websocket.close(code=1002)
        return
    client_ctx = state.clients.get(to_number)
    if client_ctx is None:
        logger.warning(
            "twilio_stream: no client for to_number=%s call_sid=%s, dropping",
            to_number, call_sid,
        )
        await websocket.close(code=1002)
        return
    t_ready = time.perf_counter()

    logger.info(
        "twilio_stream_timing call_sid=%s accept_ms=%.0f connected_ms=%.0f "
        "start_ms=%.0f ready_ms=%.0f",
        call_sid,
        (t_accept - t_entry) * 1000,
        (t_connected - t_accept) * 1000,
        (t_start - t_connected) * 1000,
        (t_ready - t_start) * 1000,
    )
    logger.info(
        "twilio_stream call_sid=%s stream_sid=%s client_id=%s caller=%s starting pipeline",
        call_sid, stream_sid, client_ctx.config.client_id,
        _redact_phone(caller_number) if caller_number else "<anonymous>",
    )
    await run_voice_session(
        websocket=websocket,
        stream_sid=stream_sid,
        call_sid=call_sid,
        client_ctx=client_ctx,
        minute_counters=state.minute_counters,
        active_calls=state.active_calls,
        caller_number=caller_number,
    )
