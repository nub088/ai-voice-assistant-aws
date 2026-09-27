"""End-to-end tests for the FastAPI app.

The Vapi-shaped /chat/completions endpoint is gone, tests now cover:

  - /healthz reports the locked voice model + prompt version
  - /twilio/voice returns valid TwiML and points at the public host
  - /twilio/voice rejects invalid Twilio signatures when TWILIO_AUTH_TOKEN is set
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _clearslope_number() -> str:
    """Read clearslope's Twilio number from the config the app actually loads,
    so this test never drifts from config/clients/clearslope.json."""
    cfg = json.loads(Path("config/clients/clearslope.json").read_text())
    return cfg["twilio_number"]


CLEARSLOPE_TO = _clearslope_number()


@pytest.fixture(autouse=True)
def _isolate_twilio_auth_token(monkeypatch):
    """Hermetic Twilio-signature tests regardless of run order.

    The first import of tools.llm_proxy.main runs load_dotenv(), which leaks the
    real .env TWILIO_AUTH_TOKEN into os.environ for the rest of the session. In
    isolation that import happens inside a test body (after its own delenv),
    silently restoring the token and flipping an expected 500 into a 403. Force
    the import here so load_dotenv() fires first, then clear the token; tests
    that need one set it explicitly and rebuild (which runs after this fixture).
    """
    import tools.llm_proxy.main  # noqa: F401  (ensure load_dotenv() has fired)

    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)


@pytest.fixture()
def app(monkeypatch):
    # Make sure the app module loads without ANTHROPIC_API_KEY / Twilio creds
    # being set, it should warn but still build.
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    for var in (
        "ANTHROPIC_API_KEY",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_ACCOUNT_SID",
        "DEEPGRAM_API_KEY",
        "CARTESIA_API_KEY",
        "CARTESIA_VOICE_ID",
        "PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    # Force a fresh build each test so app state reflects the env above.
    from tools.llm_proxy import main as main_module

    return main_module._build_app()


def test_healthz_reports_locked_voice_model(app) -> None:
    client = TestClient(app)
    resp = client.get("/healthz")
    body = resp.json()
    # Receptionist call path must be Haiku, never Opus or Sonnet.
    assert body["voice_model"].startswith("claude-haiku-"), body["voice_model"]
    # Multi-client observability fields (Phase 1, replaces single-client prompt_version).
    assert "clients" in body, body
    assert "clients_with_no_sheet" in body, body
    assert "missing" in body, body


def test_healthz_returns_503_when_required_env_missing(app, monkeypatch) -> None:
    """When upstream creds are missing at request time, /healthz must 503."""
    # Patch os.environ.get to simulate missing creds at request time, after
    # the app is already built. (load_dotenv repopulates os.environ on import,
    # so monkeypatch.delenv before app build is racy, patching the lookup
    # used inside the route is the reliable way.)
    import os as _os

    original_get = _os.environ.get
    suppressed = {"ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID", "TWILIO_AUTH_TOKEN"}

    def fake_get(name, default=None):
        if name in suppressed:
            return default
        return original_get(name, default)

    monkeypatch.setattr("tools.llm_proxy.main.os.environ.get", fake_get)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["status"] == "degraded"
    assert "anthropic" in body["missing"]


def test_twilio_voice_returns_twiml_with_stream_url(app, monkeypatch) -> None:
    # Production fails loud without TWILIO_AUTH_TOKEN. Tests opt out
    # explicitly so we can post bare form data and assert TwiML shape.
    monkeypatch.setenv("PUBLIC_BASE_URL", "ai-receptionist.fly.dev")
    client = TestClient(app)
    resp = client.post(
        "/twilio/voice",
        data={"CallSid": "CA_test_123", "To": CLEARSLOPE_TO, "From": "+15555550100"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/xml")

    root = ET.fromstring(resp.text)
    assert root.tag == "Response"
    stream = root.find("./Connect/Stream")
    assert stream is not None
    assert stream.get("url") == "wss://ai-receptionist.fly.dev/twilio/stream"

    # The caller's E.164 number must be forwarded to the Media Stream as a
    # custom parameter so the function-tool layer can text the caller back.
    params = stream.findall("./Parameter")
    by_name = {p.get("name"): p.get("value") for p in params}
    assert by_name.get("from") == "+15555550100", f"missing/wrong from param in {resp.text}"
    # SC1: the inbound To number must ride on the Stream as a parameter so
    # /twilio/stream can resolve the client without the in-process bridge
    # (machine-independent, survives multi-machine + rolling deploys).
    assert by_name.get("to") == CLEARSLOPE_TO, f"missing <Parameter name='to'> in {resp.text}"


def test_twilio_voice_omits_from_parameter_when_caller_id_blocked(app, monkeypatch) -> None:
    """Anonymous callers send no From; the Stream still carries the To param."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "ai-receptionist.fly.dev")
    client = TestClient(app)
    resp = client.post("/twilio/voice", data={"CallSid": "CA_test_123", "To": CLEARSLOPE_TO})
    assert resp.status_code == 200, resp.text
    root = ET.fromstring(resp.text)
    stream = root.find("./Connect/Stream")
    assert stream is not None
    by_name = {p.get("name"): p.get("value") for p in stream.findall("./Parameter")}
    # No caller id → no 'from' param, but 'to' (SC1 routing) is always present.
    assert "from" not in by_name, f"unexpected from param in {resp.text}"
    assert by_name.get("to") == CLEARSLOPE_TO


def test_twilio_voice_rejects_invalid_signature_when_token_set(app, monkeypatch) -> None:
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret-not-the-real-one")
    # Rebuild so app picks up the new auth_token in ClientContext.
    from tools.llm_proxy import main as main_module
    rebuilt = main_module._build_app()
    client = TestClient(rebuilt)
    resp = client.post(
        "/twilio/voice",
        data={"CallSid": "CA_test_123", "To": CLEARSLOPE_TO, "From": "+15555550100"},
        headers={"X-Twilio-Signature": "definitely-wrong"},
    )
    assert resp.status_code == 403


def test_twilio_voice_fails_when_token_missing_and_not_explicitly_allowed(monkeypatch) -> None:
    """Default secure: missing TWILIO_AUTH_TOKEN with no ALLOW_UNSIGNED_TWILIO must reject."""
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_UNSIGNED_TWILIO", raising=False)
    from tools.llm_proxy import main as main_module
    built = main_module._build_app()
    client = TestClient(built)
    resp = client.post(
        "/twilio/voice",
        data={"CallSid": "CA_test_123", "To": CLEARSLOPE_TO, "From": "+15555550100"},
    )
    assert resp.status_code == 500


def test_healthz_twilio_signature_degraded_in_prod_with_unsigned_flag(monkeypatch) -> None:
    """S4 follow-up: /healthz must use the prod-guarded check, so an
    accidentally-set ALLOW_UNSIGNED_TWILIO in production reports degraded
    (twilio_signature missing) rather than masking the misconfiguration as ok."""
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    monkeypatch.setenv("FLY_APP_NAME", "ai-receptionist")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    from tools.llm_proxy import main as main_module
    built = main_module._build_app()
    client = TestClient(built)
    body = client.get("/healthz").json()
    assert "twilio_signature" in body["missing"], body


def test_twilio_voice_ignores_unsigned_hatch_in_production(monkeypatch) -> None:
    """S4: ALLOW_UNSIGNED_TWILIO must be inert in production. With FLY_APP_NAME
    set (a real deploy) and no auth token, a forged /twilio/voice POST is
    rejected (500) even though the escape-hatch flag is on, it cannot skip
    signature validation."""
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    monkeypatch.setenv("FLY_APP_NAME", "ai-receptionist")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    from tools.llm_proxy import main as main_module
    built = main_module._build_app()
    client = TestClient(built)
    resp = client.post(
        "/twilio/voice",
        data={"CallSid": "CA_test_123", "To": CLEARSLOPE_TO, "From": "+15555550100"},
    )
    assert resp.status_code == 500


def test_chat_completions_endpoint_is_gone(app) -> None:
    """Vapi's /chat/completions surface should no longer exist."""
    client = TestClient(app)
    resp = client.post("/chat/completions", json={"messages": []})
    assert resp.status_code == 404


def test_vapi_webhook_endpoint_is_gone(app) -> None:
    client = TestClient(app)
    resp = client.post("/webhook/vapi", json={})
    assert resp.status_code == 404


# --- /auth/demo hardening (audit finding S7) -------------------------------

@pytest.fixture()
def auth_app(monkeypatch):
    """App with demo credentials configured (for /auth/demo tests)."""
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "s3cret-pw")
    for var in (
        "ANTHROPIC_API_KEY", "TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID",
        "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID", "PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    from tools.llm_proxy import main as main_module
    return main_module._build_app()


def test_auth_demo_accepts_valid_credentials(auth_app) -> None:
    client = TestClient(auth_app)
    resp = client.post("/auth/demo", json={"username": "demo", "password": "s3cret-pw"})
    assert resp.status_code == 200, resp.text
    assert "token" in resp.json()


def test_auth_demo_rejects_wrong_password(auth_app) -> None:
    client = TestClient(auth_app)
    resp = client.post("/auth/demo", json={"username": "demo", "password": "wrong"})
    assert resp.status_code == 401


def test_auth_demo_rate_limited_after_burst(auth_app) -> None:
    """Unlimited brute force must be blocked: a burst of bad logins gets 429."""
    client = TestClient(auth_app)
    # fly-client-ip is a Cloudflare edge (173.245.48.0/20), so cf-connecting-ip
    # is trusted as the real client (S6).
    codes = [
        client.post(
            "/auth/demo",
            json={"username": "x", "password": "y"},
            headers={"fly-client-ip": "173.245.48.1", "cf-connecting-ip": "203.0.113.7"},
        ).status_code
        for _ in range(8)
    ]
    assert codes[0] == 401, codes          # first attempts hit the auth check
    assert 429 in codes, codes             # the burst is throttled


def test_auth_demo_rate_limit_keyed_on_real_client_ip(auth_app) -> None:
    """Throttling keys on cf-connecting-ip, not the shared edge address, so one
    abuser can't lock out everyone and isn't masked behind Cloudflare."""
    client = TestClient(auth_app)
    cf_edge = "173.245.48.1"  # a Cloudflare edge → cf-connecting-ip trusted (S6)
    for _ in range(8):
        client.post(
            "/auth/demo",
            json={"username": "x", "password": "y"},
            headers={"fly-client-ip": cf_edge, "cf-connecting-ip": "198.51.100.1"},
        )
    # A different client IP still gets a normal auth rejection, not 429.
    resp = client.post(
        "/auth/demo",
        json={"username": "x", "password": "y"},
        headers={"fly-client-ip": cf_edge, "cf-connecting-ip": "198.51.100.2"},
    )
    assert resp.status_code == 401, resp.status_code


# --- Web hardening: headers, body limit, token sweep, IP anon (S18/S15/S13/S12)

def test_security_headers_present_on_responses(app) -> None:
    client = TestClient(app)
    resp = client.get("/healthz")
    h = resp.headers
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("x-frame-options") == "DENY"
    assert "referrer-policy" in h
    assert "strict-transport-security" in h


def test_oversized_request_body_is_rejected_413(app) -> None:
    client = TestClient(app)
    big = "x" * (2 * 1024 * 1024)  # 2 MB, over the 1 MB cap
    resp = client.post("/auth/demo", content=big,
                       headers={"content-type": "application/json"})
    assert resp.status_code == 413, resp.status_code


# --- default rate limit + Twilio exemption (audit finding S8) ---------------

def _build_app_with_default_limit(monkeypatch, limit: str = "3/minute"):
    """Build the app with a deliberately tiny default rate limit so exemption
    can be exercised in a handful of requests."""
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    monkeypatch.setenv("DEFAULT_RATE_LIMIT", limit)
    monkeypatch.setenv("VOICEMAIL_URL_SECRET", "x" * 32)  # so /vm rejects cleanly (403, not 500)
    for var in ("TWILIO_AUTH_TOKEN", "FLY_APP_NAME", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    from tools.llm_proxy import main as main_module
    return main_module._build_app()


def test_twilio_voice_exempt_from_default_rate_limit(monkeypatch) -> None:
    """S8: /twilio/* must never be rate-limited, Twilio's webhooks come from a
    shared source-IP pool, so a per-IP cap would drop real calls. Far more
    requests than the default allows must all avoid 429."""
    app = _build_app_with_default_limit(monkeypatch)
    client = TestClient(app)
    codes = [
        client.post(
            "/twilio/voice",
            data={"CallSid": f"CA{i}", "To": CLEARSLOPE_TO, "From": "+15555550100"},
        ).status_code
        for i in range(10)
    ]
    assert 429 not in codes, codes


def test_voicemail_callback_exempt_from_default_rate_limit(monkeypatch) -> None:
    """S8: the Twilio voicemail recording callback must not be throttled."""
    app = _build_app_with_default_limit(monkeypatch)
    client = TestClient(app)
    codes = [
        client.post("/twilio/voicemail/recording", data={"RecordingSid": f"RE{i}"}).status_code
        for i in range(10)
    ]
    assert 429 not in codes, codes


def test_healthz_exempt_from_default_rate_limit(monkeypatch) -> None:
    """S8: Fly's health checker hits /healthz constantly; it must not 429."""
    app = _build_app_with_default_limit(monkeypatch)
    client = TestClient(app)
    codes = [client.get("/healthz").status_code for _ in range(10)]
    assert 429 not in codes, codes


def test_vm_playback_exempt_from_default_rate_limit(monkeypatch) -> None:
    """S8: signed-token playback is already gated; exempt it from the default
    so a legitimate burst (owner replaying) isn't throttled."""
    app = _build_app_with_default_limit(monkeypatch)
    client = TestClient(app)
    codes = [client.get("/vm/REabc?exp=9999999999&sig=bad").status_code for _ in range(10)]
    assert 429 not in codes, codes


def test_default_rate_limit_is_configured(monkeypatch) -> None:
    """The default flood backstop exists (a non-exempt route would be limited)."""
    app = _build_app_with_default_limit(monkeypatch, limit="240/minute")
    assert app.state.limiter._default_limits, "expected a configured default rate limit"


# --- _real_ip trusted-proxy resolution (audit finding S6) -------------------

def _stub_request(headers: dict | None = None, client_host: str | None = None):
    """Minimal stand-in for a Starlette Request for _real_ip unit tests."""
    from types import SimpleNamespace

    lowered = {k.lower(): v for k, v in (headers or {}).items()}

    class _Headers:
        def get(self, key, default=""):
            return lowered.get(key.lower(), default)

    client = SimpleNamespace(host=client_host) if client_host else None
    return SimpleNamespace(headers=_Headers(), client=client)


def test_real_ip_uses_forwarded_client_when_edge_is_trusted_proxy() -> None:
    """When the edge address (Fly-Client-IP) is a Cloudflare IP, the request
    genuinely transited Cloudflare, so cf-connecting-ip is the real client."""
    from tools.llm_proxy.main import _real_ip

    req = _stub_request({"fly-client-ip": "173.245.48.10", "cf-connecting-ip": "9.9.9.9"})
    assert _real_ip(req) == "9.9.9.9"


def test_real_ip_ignores_forged_cf_header_from_untrusted_edge() -> None:
    """S6: a client hitting *.fly.dev directly (edge not a trusted proxy) can
    set any cf-connecting-ip; it must be ignored so rate limits can't be evaded
    by rotating the header. The unforgeable edge address is used instead."""
    from tools.llm_proxy.main import _real_ip

    req = _stub_request({"fly-client-ip": "5.5.5.5", "cf-connecting-ip": "1.2.3.4"})
    assert _real_ip(req) == "5.5.5.5"


def test_real_ip_ignores_forged_xff_from_untrusted_edge() -> None:
    from tools.llm_proxy.main import _real_ip

    req = _stub_request({"fly-client-ip": "5.5.5.5", "x-forwarded-for": "1.2.3.4, 6.6.6.6"})
    assert _real_ip(req) == "5.5.5.5"


def test_real_ip_falls_back_to_peer_without_edge_info() -> None:
    from tools.llm_proxy.main import _real_ip

    req = _stub_request({}, client_host="127.0.0.1")
    assert _real_ip(req) == "127.0.0.1"


def test_real_ip_trusted_ranges_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
    from tools.llm_proxy.main import _real_ip

    req = _stub_request({"fly-client-ip": "10.1.2.3", "cf-connecting-ip": "9.9.9.9"})
    assert _real_ip(req) == "9.9.9.9"
    # An edge outside the configured range no longer trusts the header.
    req2 = _stub_request({"fly-client-ip": "8.8.8.8", "cf-connecting-ip": "9.9.9.9"})
    assert _real_ip(req2) == "8.8.8.8"


def test_anonymize_ip_masks_host_portion() -> None:
    from tools.llm_proxy.main import _anonymize_ip
    assert _anonymize_ip("203.0.113.45") == "203.0.113.0"          # IPv4 → /24
    assert _anonymize_ip("2001:db8:1:2:3:4:5:6").startswith("2001:db8:")  # IPv6 truncated
    assert _anonymize_ip("testclient") == "testclient"             # non-IP passthrough


def test_sweep_expired_tokens_removes_only_expired() -> None:
    from datetime import datetime, timedelta
    from tools.llm_proxy.main import _sweep_expired_tokens

    now = datetime(2026, 1, 1, 12, 0, 0)
    tokens = {
        "live": now + timedelta(hours=1),
        "dead": now - timedelta(seconds=1),
    }
    removed = _sweep_expired_tokens(tokens, now)
    assert removed == 1
    assert "live" in tokens
    assert "dead" not in tokens


# --- startup warm-up scope (audit findings P2 / P3) --------------------------

def test_warmup_does_not_construct_smart_turn() -> None:
    """P2: LocalSmartTurnAnalyzerV3 is unused by any pipeline, warming it
    burned ~4s of startup and pinned an ONNX graph in RAM for nothing."""
    import inspect
    from tools.llm_proxy import main as main_mod
    src = inspect.getsource(main_mod._warm_pipecat_models)
    assert "LocalSmartTurnAnalyzerV3" not in src
    assert "warmed_turn_analyzer" not in src


def test_warmup_primes_pii_redactor() -> None:
    """P3: the first post-restart call-end paid a multi-second spaCy model
    load in the hangup path; startup warm-up must prime it instead."""
    import inspect
    from tools.llm_proxy import main as main_mod
    src = inspect.getsource(main_mod._warm_pipecat_models)
    assert "_get_redactor" in src


# --- demo token revocation (audit finding S13) -------------------------------

def test_auth_logout_revokes_token(auth_app) -> None:
    """S13: a leaked demo token must be revocable before its 8h TTL."""
    client = TestClient(auth_app)
    resp = client.post("/auth/demo", json={"username": "demo", "password": "s3cret-pw"})
    token = resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.post("/auth/logout", headers=headers)
    assert resp.status_code == 200, resp.text

    # The revoked token must now be rejected by /text-chat (401 fires at token
    # validation, before any Anthropic call, so no API key is needed).
    resp = client.post(
        "/text-chat", headers=headers,
        json={"session_id": "s1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401, resp.status_code


def test_auth_logout_without_token_is_idempotent_noop(auth_app) -> None:
    """Unknown/absent tokens return 200 so the widget can always clear state."""
    client = TestClient(auth_app)
    assert client.post("/auth/logout").status_code == 200
    assert client.post(
        "/auth/logout", headers={"Authorization": "Bearer nonsense"}
    ).status_code == 200


def test_chunked_body_without_content_length_is_capped(monkeypatch) -> None:
    """S15: a streamed/chunked body (no Content-Length header) must be rejected
    at the cap, not buffered until OOM. The header check alone can't see it."""
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    monkeypatch.setenv("MAX_REQUEST_BYTES", "1024")
    for var in (
        "ANTHROPIC_API_KEY", "TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID",
        "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID", "PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    from tools.llm_proxy import main as main_module
    client = TestClient(main_module._build_app())

    def body_gen():
        for _ in range(64):
            yield b"x" * 64  # 4 KiB total, sent chunked (no Content-Length)

    resp = client.post(
        "/auth/demo", content=body_gen(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413, resp.status_code
