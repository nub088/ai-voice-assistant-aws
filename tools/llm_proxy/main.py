"""
FastAPI app for the AI receptionist.

Two parallel surfaces share the same loaded system prompt and selected model:

  • Voice path, `/twilio/voice` (TwiML) + `/twilio/stream` (WebSocket).
                  Twilio Programmable Voice + Pipecat (Deepgram STT,
                  Anthropic or Bedrock LLM, Cartesia TTS). See `voice_routes.py`.

  • Demo path, `/auth/demo`, `/text-chat`. The web demo widget at
                  clearslopedigital.com/demo. Stateless text-only model calls.

Run locally:
    uvicorn tools.llm_proxy.main:app --host 0.0.0.0 --port 8080 --reload

Deploy to fly.io: see `infra/fly/`.
"""
from __future__ import annotations

import csv
import functools
import ipaddress
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse as StarletteJSONResponse

from . import transcript_store
from .aws_config import load_aws_secrets
from .client_config import ClientContext, scan_client_configs
from .logging_config import setup_logging
from .prompt_loader import load_prompt
from .sheets_writer import VOICEMAIL_HEADER, VOICEMAIL_TAB_NAME, writer_from_env
from .sms_notifier import notifier_from_env
from .text_chat_handler import handle_text_chat, TextChatRequest
from .twilio_auth import unsigned_twilio_allowed
from .voice_pipeline import RECEPTIONIST_MODEL
from .voice_routes import router as voice_router
from .voicemail import PLAYBACK_TOKEN_EXPIRY_DAYS, router as voicemail_router
from .voicemail_urls import sign_playback_url

load_dotenv()
load_aws_secrets()

setup_logging()
logger = logging.getLogger("llm_proxy")


def _warm_pipecat_models(app: FastAPI) -> None:
    """Pre-load the Silero VAD model at app startup.

    Without this, the first inbound call after process restart paid ~10 s of
    cold-start cost (module imports + ONNX graph init on shared-cpu-1x),
    pushing Media Stream WS setup past Twilio's tolerance and producing the
    "3 beeps then disconnect" symptom (Twilio error 31921). See
    call_drops_diagnosis.txt at the project root for the evidence trail.

    Constructing one throwaway analyzer here loads the Silero weights into
    the OS page cache and primes module imports; per-call code still builds a
    fresh SileroVADAnalyzer (VAD audio-history buffers must not be shared
    between concurrent callers) but each construction is ~100 ms instead of
    ~5 s. The Smart Turn analyzer is NOT warmed, no pipeline uses it (P2).
    """
    import time as _time
    t0 = _time.perf_counter()
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    t_import = _time.perf_counter()
    SileroVADAnalyzer()
    t_vad = _time.perf_counter()
    logger.info(
        "pipecat_warm_done import_ms=%.0f vad_ms=%.0f total_ms=%.0f",
        (t_import - t0) * 1000,
        (t_vad - t_import) * 1000,
        (t_vad - t0) * 1000,
    )

    # P3: prime the PII redactor (Presidio AnalyzerEngine + spaCy model) so the
    # first call-end after a restart doesn't pay the multi-second model load in
    # the hangup path. _get_redactor caches both success and failure process-wide
    # and returns None when redaction isn't configured, harmless in dev/tests.
    from .voice_pipeline import _get_redactor
    t_red0 = _time.perf_counter()
    _get_redactor()
    logger.info("redactor_warm_done ms=%.0f", (_time.perf_counter() - t_red0) * 1000)


def _init_sentry() -> bool:
    """Initialize Sentry if SENTRY_DSN is set. Returns True when wired up.

    Sentry is opt-in, when DSN is unset, this is a no-op and the SDK is
    not required. If the SDK isn't installed, log a warning and skip.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN set but sentry-sdk not installed, skipping")
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENV", "production"),
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        send_default_pii=False,
    )
    return True


_DEMO_LOG = Path(__file__).resolve().parents[2] / ".tmp" / "demo_visitors.csv"
_DEMO_LOG_FIELDS = ["timestamp", "ip", "event", "session_id"]


def _log_visitor(ip: str, event: str, session_id: str = "") -> None:
    _DEMO_LOG.parent.mkdir(parents=True, exist_ok=True)
    write_header = not _DEMO_LOG.exists()
    with _DEMO_LOG.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_DEMO_LOG_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            # Store only the anonymized IP (S12), this CSV is retained
            # indefinitely, so the full address must never land here.
            "ip": _anonymize_ip(ip),
            "event": event,
            "session_id": session_id,
        })


# Cloudflare's published edge ranges (https://www.cloudflare.com/ips/). When the
# request's edge address falls in one of these, the request genuinely transited
# Cloudflare and its cf-connecting-ip / X-Forwarded-For are trustworthy. Override
# with TRUSTED_PROXY_IPS (comma-separated CIDRs) if the fronting proxy changes.
_DEFAULT_TRUSTED_PROXY_CIDRS = (
    "173.245.48.0/20,103.21.244.0/22,103.22.200.0/22,103.31.4.0/22,"
    "141.101.64.0/18,108.162.192.0/18,190.93.240.0/20,188.114.96.0/20,"
    "197.234.240.0/22,198.41.128.0/17,162.158.0.0/15,104.16.0.0/13,"
    "104.24.0.0/14,172.64.0.0/13,131.0.72.0/22,"
    "2400:cb00::/32,2606:4700::/32,2803:f800::/32,2405:b500::/32,"
    "2405:8100::/32,2a06:98c0::/29,2c0f:f248::/32"
)


@functools.lru_cache(maxsize=8)
def _parse_proxy_networks(spec: str) -> tuple:
    nets = []
    for cidr in spec.split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            logger.warning("ignoring invalid TRUSTED_PROXY_IPS entry: %r", cidr)
    return tuple(nets)


def _trusted_proxy_networks() -> tuple:
    spec = os.environ.get("TRUSTED_PROXY_IPS", "").strip() or _DEFAULT_TRUSTED_PROXY_CIDRS
    return _parse_proxy_networks(spec)


def _edge_ip(request: Request) -> str:
    """The address that connected to our edge. Fly sets Fly-Client-IP and a
    client cannot override it through Fly's proxy, so it is the unforgeable
    basis for trust decisions. Falls back to the socket peer in local dev."""
    return (
        request.headers.get("fly-client-ip", "").strip()
        or (request.client.host if request.client else "")
    )


def _is_trusted_edge(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    return any(addr in net for net in _trusted_proxy_networks())


def _real_ip(request: Request) -> str:
    """Resolve the true client IP for rate-limiting / logging (S6, S7).

    Forwarded headers (cf-connecting-ip, X-Forwarded-For) are honored ONLY when
    the edge address is a trusted proxy (Cloudflare); otherwise a client hitting
    *.fly.dev directly could forge them to evade per-IP rate limits or frame
    another address. When the edge is untrusted, the unforgeable edge IP is used.
    """
    edge = _edge_ip(request)
    if os.environ.get("AWS_ALB_PROXY") == "1" and _is_trusted_edge(edge):
        # An ALB appends the actual peer to X-Forwarded-For. Any earlier
        # entries may have been supplied by the caller.
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        if forwarded:
            return forwarded
    if edge and _is_trusted_edge(edge):
        forwarded = (
            request.headers.get("cf-connecting-ip", "").strip()
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        )
        if forwarded:
            return forwarded
    return edge or "unknown"


def _anonymize_ip(ip: str) -> str:
    """Mask the host portion of an IP before it's stored/retained (S12, GDPR
    data-minimization). IPv4 -> /24 (last octet zeroed), IPv6 -> /48. Non-IP
    strings (e.g. 'testclient', 'unknown') pass through unchanged."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return ip
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False).network_address)


class BodyTooLarge(Exception):
    """Raised by the body-cap receive wrapper when a streamed body exceeds
    MAX_REQUEST_BYTES (S15), aborting body parsing inside the endpoint."""


class _BodySizeLimitMiddleware:
    """Pure-ASGI middleware: counts streamed request-body bytes and rejects
    past the cap with 413 (S15). Complements, does not replace, the cheap
    Content-Length header check in the HTTP middleware: a chunked POST
    carries no Content-Length and would otherwise buffer until OOM.

    The 413 is sent directly from the receive wrapper (before the endpoint
    has produced any response), because endpoints wrap body parsing in
    `except Exception` and would otherwise swallow the abort and answer with
    their own status; any response the app produces afterwards is dropped.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        received = 0
        responded = False

        async def limited_receive():
            nonlocal received, responded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes and not responded:
                    responded = True
                    await send({
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"application/json")],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b'{"error": "request body too large"}',
                    })
                    raise BodyTooLarge()
            return message

        async def guarded_send(message):
            # Drop whatever the app tries to send after we already 413'd,             # an endpoint that caught BodyTooLarge will still try to respond.
            if responded:
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except BodyTooLarge:
            pass  # 413 already sent above


def _sweep_expired_tokens(tokens: dict, now: datetime) -> int:
    """Evict expired demo tokens; returns the count removed (S13).

    The token store was only pruned lazily on a hit, so tokens that were issued
    but never used (or used once) lingered until expiry with no eviction,     unbounded growth. Called opportunistically when a new token is issued.
    """
    expired = [t for t, exp in tokens.items() if exp <= now]
    for t in expired:
        tokens.pop(t, None)
    return len(expired)


def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> StarletteJSONResponse:
    return StarletteJSONResponse(status_code=429, content={"error": "rate limit exceeded, try again later"})


def _build_app() -> FastAPI:
    app = FastAPI(title="AI Receptionist", version="0.2.0")
    if _init_sentry():
        logger.info("sentry initialized (env=%s)", os.environ.get("SENTRY_ENV", "production"))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://clearslopedigital.com",
            "https://www.clearslopedigital.com",
            "http://localhost:4321",
            "http://localhost:3000",
        ],
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

    # Key rate limits on the true client IP (cf-connecting-ip first), not the
    # shared Cloudflare edge address, otherwise one abuser throttles everyone
    # and per-IP limits are trivially bypassed/over-broad behind the proxy (S7).
    # S8: a generous per-IP default rate limit guards every endpoint as a
    # flood backstop. Override via DEFAULT_RATE_LIMIT (e.g. "120/minute"), or
    # "off" to disable. The Twilio webhook + health paths are exempted below,     # Twilio's webhooks share a source-IP pool (a per-IP cap would drop real
    # calls) and Fly hits /healthz constantly.
    default_limit = os.environ.get("DEFAULT_RATE_LIMIT", "240/minute").strip()
    default_limits = [] if default_limit.lower() in {"", "off", "none"} else [default_limit]
    limiter = Limiter(key_func=_real_ip, default_limits=default_limits)
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)

    # Reject oversized bodies (S15). Twilio webhooks + demo chat are small; 1 MB
    # is generous. Override via MAX_REQUEST_BYTES.
    try:
        max_request_bytes = int(os.environ.get("MAX_REQUEST_BYTES", str(1024 * 1024)))
    except ValueError:
        max_request_bytes = 1024 * 1024

    # S15 (streamed/chunked case): count body bytes as they arrive and 413
    # past the cap, the Content-Length fast path below can't see a chunked
    # body. The exception raised mid-parse is converted before any response
    # has started.
    app.add_middleware(_BodySizeLimitMiddleware, max_bytes=max_request_bytes)

    @app.middleware("http")
    async def _log_every_request(request: Request, call_next):
        # S15: cap request body size by Content-Length before doing any work.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_request_bytes:
                    return StarletteJSONResponse(
                        status_code=413, content={"error": "request body too large"}
                    )
            except ValueError:
                pass
        ua = request.headers.get("user-agent", "")
        ip = _real_ip(request)  # trusted-proxy resolution; not a forgeable raw header (S6)
        logger.info("REQ %s %s ua=%r ip=%s", request.method, request.url.path, ua, ip)
        response = await call_next(request)
        # S18: baseline security response headers (setdefault so a route can
        # override). HSTS is meaningful because fly.toml forces HTTPS.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
        logger.info("RES %s %s -> %d", request.method, request.url.path, response.status_code)
        return response

    # Multi-client startup (D-02): scan config/clients/*.json, build one ClientContext per client.
    client_configs = scan_client_configs(Path("config/clients"))
    if not client_configs:
        raise RuntimeError("no client configs found in config/clients/, refusing to start")

    global_auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not global_auth_token and not unsigned_twilio_allowed():
        logger.warning(
            "TWILIO_AUTH_TOKEN not set and ALLOW_UNSIGNED_TWILIO not enabled "
            "(or refused in production), Twilio webhook signature validation "
            "will fail in production."
        )

    clients: dict[str, ClientContext] = {}
    for twilio_number, cfg in client_configs.items():
        loaded = load_prompt(config=cfg)
        logger.info(
            "client=%s prefix=%s: prompt %s (%d chars)",
            cfg.client_id, cfg.env_prefix, loaded.version, len(loaded.text),
        )

        writer = writer_from_env(prefix=cfg.env_prefix)
        if writer is None:
            logger.warning(
                "client=%s prefix=%s: SheetsWriter not configured, "
                "missing GOOGLE_SERVICE_ACCOUNT_FILE and/or %s_GOOGLE_SHEET_ID.",
                cfg.client_id, cfg.env_prefix, cfg.env_prefix,
            )
        else:
            logger.info(
                "client=%s prefix=%s: SheetsWriter ready (sheet=%s)",
                cfg.client_id, cfg.env_prefix,
                os.environ.get(f"{cfg.env_prefix}_GOOGLE_SHEET_ID"),
            )

        notifier = notifier_from_env(
            prefix=cfg.env_prefix,
            business_name=cfg.business.name,
            business_short_name=cfg.business.short_name,
        )
        if notifier is None:
            logger.info(
                "client=%s prefix=%s: SMS notifier not configured, "
                "set %s_TWILIO_SMS_FROM to enable.",
                cfg.client_id, cfg.env_prefix, cfg.env_prefix,
            )
        else:
            logger.info(
                "client=%s prefix=%s: SMS notifier ready (from=%s, owner_alerts=%s)",
                cfg.client_id, cfg.env_prefix,
                notifier.from_number,
                "yes" if notifier.owner_number else "no",
            )

        clients[twilio_number] = ClientContext(
            config=cfg,
            loaded_prompt=loaded,
            sheets_writer=writer,
            sms_notifier=notifier,
            auth_token=global_auth_token,
        )

    logger.info("client registry ready: %d client(s), %s", len(clients),
                [cfg.client_id for cfg in client_configs.values()])

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("BEDROCK_MODEL_ID"):
        logger.warning("ANTHROPIC_API_KEY not set, voice + /text-chat will fail until configured.")

    # AsyncAnthropic for the /text-chat (demo) path. The voice path constructs
    # its own AnthropicLLMService inside Pipecat.
    client = None if os.environ.get("BEDROCK_MODEL_ID") else anthropic.AsyncAnthropic()

    if not os.environ.get("DEEPGRAM_API_KEY"):
        logger.warning("DEEPGRAM_API_KEY not set, voice path will reject calls.")
    if not os.environ.get("CARTESIA_API_KEY") or not os.environ.get("CARTESIA_VOICE_ID"):
        logger.warning("CARTESIA_API_KEY or CARTESIA_VOICE_ID not set, voice path will reject calls.")

    demo_username = os.environ.get("DEMO_USERNAME", "").strip()
    demo_password = os.environ.get("DEMO_PASSWORD", "").strip()
    if not demo_username or not demo_password:
        logger.warning("DEMO_USERNAME or DEMO_PASSWORD not set, /auth/demo will reject all logins.")

    # For the /text-chat demo, use the clearslope client's prompt (single-tenant demo path).
    _demo_ctx = next(
        (ctx for ctx in clients.values() if ctx.config.client_id == "clearslope"),
        next(iter(clients.values())),
    )

    # Stash for voice_routes (D-03): per-call factories read from app.state.
    app.state.clients = clients                                        # dict[twilio_number, ClientContext]
    # Minute counters survive process restarts via SQLite, load any persisted
    # totals and default to 0.0 for clients with no prior history.
    persisted_counters = transcript_store.load_minute_counters()
    app.state.minute_counters = {
        cfg.client_id: persisted_counters.get(cfg.client_id, 0.0)
        for cfg in client_configs.values()
    }                                                                  # dict[client_id, float]
    if persisted_counters:
        logger.info(
            "loaded persisted minute counters: %s",
            {cid: round(v, 1) for cid, v in app.state.minute_counters.items()},
        )
    app.state.pending_calls: dict[str, str] = {}                      # dict[call_sid, twilio_number]
    app.state.pending_calls_started_at: dict[str, float] = {}         # dict[call_sid, monotonic seconds]
    app.state.active_calls: dict[str, int] = {}                       # dict[client_id, in-flight count] (SC5)

    # In-memory token store: token → expiry. Tokens last 8 hours.
    _demo_tokens: dict[str, datetime] = {}
    _TOKEN_TTL = timedelta(hours=8)

    def _issue_token() -> str:
        # S13: opportunistically evict expired tokens so the store stays bounded.
        _sweep_expired_tokens(_demo_tokens, datetime.utcnow())
        token = secrets.token_urlsafe(32)
        _demo_tokens[token] = datetime.utcnow() + _TOKEN_TTL
        return token

    def _validate_token(token: str) -> bool:
        expiry = _demo_tokens.get(token)
        if expiry is None:
            return False
        if datetime.utcnow() > expiry:
            del _demo_tokens[token]
            return False
        return True

    @app.get("/healthz")
    async def healthz() -> Any:
        # Required for the voice path to actually work end-to-end. Missing
        # any of these means inbound calls will fail, the healthcheck must
        # return 503 so platforms (Fly health probes, uptime monitors) flag
        # the deploy as broken instead of trusting a stale "ok".
        # Only live clients count toward readiness, template tenants
        # (live=false) are registered for routing tests but their missing
        # Sheets/SMS wiring is expected, not a deploy failure.
        clients_with_no_sheet = [
            ctx.config.client_id
            for ctx in app.state.clients.values()
            if ctx.config.live and ctx.sheets_writer is None
        ]
        required = {
            ("bedrock" if os.environ.get("BEDROCK_MODEL_ID") else "anthropic"): bool(
                os.environ.get("BEDROCK_MODEL_ID") or os.environ.get("ANTHROPIC_API_KEY")
            ),
            "deepgram": bool(os.environ.get("DEEPGRAM_API_KEY")),
            "cartesia": bool(os.environ.get("CARTESIA_API_KEY") and os.environ.get("CARTESIA_VOICE_ID")),
            "twilio_signature": bool(os.environ.get("TWILIO_AUTH_TOKEN")) or unsigned_twilio_allowed(),
            "sheets_writer": not clients_with_no_sheet,
            "voicemail_secret": bool(os.environ.get("VOICEMAIL_URL_SECRET")),
            "public_base_url": bool(os.environ.get("PUBLIC_BASE_URL")),
        }
        if os.environ.get("AWS_ALB_PROXY") == "1":
            required["aws_storage"] = bool(
                os.environ.get("AWS_CALLS_TABLE") and os.environ.get("AWS_TRANSCRIPT_BUCKET")
            )
        missing = [name for name, ok in required.items() if not ok]
        body: dict[str, Any] = {
            "status": "ok" if not missing else "degraded",
            "clients": list(app.state.clients.keys()),
            "voice_model": RECEPTIONIST_MODEL,
            "clients_with_no_sheet": clients_with_no_sheet,
            "missing": missing,
        }
        if missing:
            return JSONResponse(status_code=503, content=body)
        return body

    @app.post("/auth/demo")
    @limiter.limit("5/minute;20/hour")
    async def auth_demo(request: Request) -> Any:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON")
        # Constant-time credential check (S7). Both comparisons run
        # unconditionally (evaluated before the branch) so response timing can't
        # reveal whether the username or the password was wrong, and
        # compare_digest avoids the early-exit timing leak of `==` on str.
        username_ok = bool(demo_username) and secrets.compare_digest(
            str(data.get("username", "")), demo_username
        )
        password_ok = bool(demo_password) and secrets.compare_digest(
            str(data.get("password", "")), demo_password
        )
        if not (username_ok and password_ok):
            raise HTTPException(status_code=401, detail="invalid credentials")
        token = _issue_token()
        ip = _real_ip(request)
        logger.info("auth_demo: login from %s", ip)
        _log_visitor(ip, "login")
        return JSONResponse({"token": token})

    @app.post("/auth/logout")
    async def auth_logout(authorization: str | None = Header(default=None)) -> Any:
        """Revoke a demo bearer token (S13). Idempotent: unknown/absent tokens
        return 200 so the widget can always clear local state."""
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:]
        _demo_tokens.pop(token, None)
        return JSONResponse({"ok": True})

    @app.post("/text-chat")
    @limiter.limit("30/hour")
    async def text_chat(
        request: Request,
        body: TextChatRequest,
        authorization: str | None = Header(default=None),
    ) -> Any:
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:]
        if not token or not _validate_token(token):
            raise HTTPException(status_code=401, detail="invalid or expired token")
        ip = _real_ip(request)
        logger.info("text_chat session=%s msgs=%d ip=%s", body.session_id[:16], len(body.messages), ip)
        if len(body.messages) == 1:
            _log_visitor(ip, "chat_start", body.session_id[:16])
        result = await handle_text_chat(client, _demo_ctx.loaded_prompt, body)
        return JSONResponse(result.model_dump())

    app.include_router(voice_router)

    app.include_router(voicemail_router)

    # S8: exempt the call-critical + health paths from the default rate limit.
    # Twilio webhooks share a source-IP pool (a per-IP cap would drop real
    # calls); Fly's health checker hammers /healthz; /vm playback is already
    # signed-token gated. Done by qualified endpoint name (slowapi's exemption
    # key) so it covers every current and future route under these prefixes.
    _rate_limit_exempt_prefixes = ("/twilio", "/vm", "/healthz")
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint and getattr(route, "path", "").startswith(_rate_limit_exempt_prefixes):
            limiter._exempt_routes.add(f"{endpoint.__module__}.{endpoint.__name__}")

    # Shared per-recording state used by the recording/transcription callbacks.
    app.state.voicemail_pending: dict[str, Any] = {}
    app.state.voicemail_transcripts: dict[str, str] = {}
    app.state.voicemail_pending_started_at: dict[str, float] = {}

    # v1 supports one voicemail-handling client. The recording/transcription
    # callbacks lack a `To` header, so we can't dispatch them per-client
    # without registering distinct callback URLs. Pick the first live client
    # (or the first registered tenant if none are live).
    _vm_ctx = next(
        (ctx for ctx in clients.values() if ctx.config.live),
        next(iter(clients.values())),
    )
    app.state.voicemail_sheets_writer = _vm_ctx.sheets_writer
    app.state.voicemail_sms_notifier = _vm_ctx.sms_notifier

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
        if not voicemail_secret or not public_base:
            return "(playback URL unavailable, server misconfigured)"
        exp = int((datetime.now(timezone.utc) + timedelta(days=PLAYBACK_TOKEN_EXPIRY_DAYS)).timestamp())
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

    # Ensure the Voicemails tab exists for each live client's sheet.
    for ctx in clients.values():
        if ctx.config.live and ctx.sheets_writer is not None:
            try:
                ctx.sheets_writer.ensure_tab(VOICEMAIL_TAB_NAME, VOICEMAIL_HEADER)
            except Exception:
                logger.exception(
                    "voicemail tab ensure failed client=%s", ctx.config.client_id
                )

    # S3: enforce the published transcript-retention window. The SQLite store is
    # on the app machine's volume, which is single-attach, the daily `retention`
    # process can't mount it, so the purge must run here, in the volume-owning
    # process. Runs once per process start (i.e. every deploy/restart);
    # best-effort so a purge failure never blocks boot. Set
    # TRANSCRIPT_RETENTION_DAYS to override the 90-day default.
    try:
        retention_days = int(os.environ.get("TRANSCRIPT_RETENTION_DAYS", "90"))
        purged = transcript_store.purge_expired(retention_days)
        if purged:
            logger.info(
                "startup transcript purge: deleted %d rows older than %dd",
                purged, retention_days,
            )
    except Exception:
        logger.exception("startup transcript purge failed")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error on %s %s", request.method, request.url)
        return JSONResponse(status_code=500, content={"error": "internal error"})

    # Synchronous warm-up: blocks uvicorn from binding to :8080 until models
    # are loaded, so Fly's first traffic only hits a hot process. ~10s on a
    # fresh deploy, ~0s thereafter (machine never auto-stops per fly.toml).
    _warm_pipecat_models(app)

    return app


app = _build_app()
