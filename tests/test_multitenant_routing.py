"""Phase-1 multi-tenant routing regression suite.

Locks in the contracts established by Plans 01-03:
  - To-based client lookup (D-01)
  - Ceiling enforcement before pipeline start (D-09)
  - math.ceil minute accounting in finally block (D-10)
  - Per-client env-prefix factory wiring (D-04, D-06)
  - Twilio signature validator's auth_token plumbing (D-12)

No real Twilio / Anthropic / Google calls, uses TestClient and direct
module inspection.
"""
from __future__ import annotations

import json
import math
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _cfg_number(client_id: str) -> str:
    """Read a client's Twilio number from the same config the app loads, so
    these tests never drift from config/clients/*.json."""
    for p in Path("config/clients").glob("*.json"):
        cfg = json.loads(p.read_text())
        if cfg.get("client_id") == client_id:
            return cfg["twilio_number"]
    raise RuntimeError(f"no config found for client_id={client_id!r}")


CLEARSLOPE_TO = _cfg_number("clearslope")
ALLGREEN_TO = _cfg_number("allgreen")


@pytest.fixture()
def app(monkeypatch):
    """Build a fresh app with no real creds; ALLOW_UNSIGNED_TWILIO=1 for routing tests."""
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    monkeypatch.setenv("PUBLIC_BASE_URL", "test.fly.dev")
    for var in (
        "ANTHROPIC_API_KEY",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_ACCOUNT_SID",
        "DEEPGRAM_API_KEY",
        "CARTESIA_API_KEY",
        "CARTESIA_VOICE_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    from tools.llm_proxy import main as main_module
    return main_module._build_app()


def test_unknown_to_returns_404(app) -> None:
    """POST /twilio/voice with an unregistered To number must return 404 (D-01)."""
    client = TestClient(app)
    resp = client.post(
        "/twilio/voice",
        data={"CallSid": "CA_unknown", "To": "+19999999999", "From": "+15550001111"},
    )
    assert resp.status_code == 404, resp.text


def test_ceiling_rejection_returns_hangup_twiml(app) -> None:
    """Calls exceeding the minute ceiling get TwiML Hangup before the pipeline starts (D-09)."""
    # Push allgreen counter to its ceiling (200 minutes).
    app.state.minute_counters["allgreen"] = 200.0

    client = TestClient(app)
    resp = client.post(
        "/twilio/voice",
        data={"CallSid": "CA_ceiling", "To": ALLGREEN_TO, "From": "+15550002222"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/xml")

    root = ET.fromstring(resp.text)
    assert root.tag == "Response"
    # Must NOT open a Media Stream, ceiling fires before streaming.
    assert root.find("./Connect/Stream") is None, f"stream opened despite ceiling: {resp.text}"
    # Must terminate the call.
    assert root.find(".//Hangup") is not None, f"no Hangup in: {resp.text}"


def test_ceiling_exceeded_counts_inflight_reservations() -> None:
    """SC5: admission must count in-flight calls (awaiting-stream in
    pending_calls + streaming in active_calls), not just completed minutes, so
    a burst of concurrent calls can't all pass a stale 'used' check."""
    from tools.llm_proxy.voice_routes import _ceiling_exceeded

    to = "+15550000001"
    cid = "acme"
    # Under ceiling, nothing in flight → admit.
    assert _ceiling_exceeded(0.0, {}, {}, to, cid, 10) is False
    # Used alone at the cap → reject.
    assert _ceiling_exceeded(10.0, {}, {}, to, cid, 10) is True
    # Three awaiting-stream calls for this number push it to the cap.
    pending = {"CA1": to, "CA2": to, "CA3": to}
    assert _ceiling_exceeded(7.0, pending, {}, to, cid, 10) is True   # 7 + 3 >= 10
    assert _ceiling_exceeded(6.0, pending, {}, to, cid, 10) is False  # 6 + 3 < 10
    # Streaming calls (active_calls) count too.
    assert _ceiling_exceeded(8.0, {}, {cid: 2}, to, cid, 10) is True  # 8 + 2 >= 10
    # Pending calls for a DIFFERENT number must not count against this client.
    assert _ceiling_exceeded(6.0, {"CAx": "+19998887777"}, {}, to, cid, 10) is False
    # No ceiling configured → never exceeded.
    assert _ceiling_exceeded(100.0, pending, {cid: 50}, to, cid, None) is False


def test_twilio_voice_rejects_when_inflight_reservations_fill_ceiling(app) -> None:
    """SC5 wiring: with used + in-flight reservations at the cap, a new call is
    rejected with Hangup TwiML and never opens a Media Stream, even though
    'used' alone is below the ceiling."""
    app.state.minute_counters["allgreen"] = 199.0
    app.state.active_calls["allgreen"] = 1  # one call already streaming
    client = TestClient(app)
    resp = client.post(
        "/twilio/voice",
        data={"CallSid": "CA_resv", "To": ALLGREEN_TO, "From": "+15550009999"},
    )
    assert resp.status_code == 200, resp.text
    root = ET.fromstring(resp.text)
    # used(199) alone < ceiling(200), but +1 streaming reservation hits the cap.
    assert root.find(".//Hangup") is not None, f"should reject (199+1>=200): {resp.text}"
    assert root.find("./Connect/Stream") is None, f"must not stream: {resp.text}"


def test_minute_counter_uses_math_ceil(app) -> None:
    """Minute accounting must use math.ceil, a 61-second call charges 2 minutes (D-10)."""
    assert math.ceil(61 / 60) == 2
    assert math.ceil(60 / 60) == 1
    assert math.ceil(1 / 60) == 1   # sub-minute call still charges 1 minute
    assert math.ceil(120 / 60) == 2  # exactly 2 minutes → 2 charged


def test_pending_calls_bridge(app) -> None:
    """POST /twilio/voice must register call_sid → to_number in pending_calls (D-01, D-03)."""
    client = TestClient(app)
    resp = client.post(
        "/twilio/voice",
        data={"CallSid": "CA_bridge_test", "To": CLEARSLOPE_TO, "From": "+15550003333"},
    )
    assert resp.status_code == 200, resp.text
    assert "CA_bridge_test" in app.state.pending_calls, (
        f"call_sid not stored in pending_calls; state: {dict(app.state.pending_calls)}"
    )
    assert app.state.pending_calls["CA_bridge_test"] == CLEARSLOPE_TO
    # Bridge entry must carry a monotonic timestamp so stale entries can be pruned.
    assert "CA_bridge_test" in app.state.pending_calls_started_at
    assert isinstance(app.state.pending_calls_started_at["CA_bridge_test"], float)


def test_twilio_voice_twiml_carries_to_stream_parameter(app) -> None:
    """SC1: /twilio/voice must put the inbound To number on the Media Stream as
    a <Parameter> so /twilio/stream can resolve the client without the
    in-process bridge (which breaks across machines / rolling deploys)."""
    client = TestClient(app)
    resp = client.post(
        "/twilio/voice",
        data={"CallSid": "CA_to_param", "To": CLEARSLOPE_TO, "From": "+15550003333"},
    )
    assert resp.status_code == 200, resp.text
    root = ET.fromstring(resp.text)
    by_name = {p.get("name"): p.get("value") for p in root.findall("./Connect/Stream/Parameter")}
    assert by_name.get("to") == CLEARSLOPE_TO, f"missing to param: {resp.text}"


class _Ctx:
    """Minimal stand-in for ClientContext (only .auth_token is read here)."""
    def __init__(self, auth_token: str) -> None:
        self.auth_token = auth_token


def test_routing_sig_round_trips_and_rejects_tampering() -> None:
    """S10: the stream routing param is HMAC-signed with the client's auth
    token so /twilio/stream can't be driven by a forged 'to' (which would start
    a paid voice session bypassing the /twilio/voice signature check)."""
    from tools.llm_proxy.voice_routes import _routing_sig, _verify_routing_sig

    secret = "client-auth-token"
    exp = 2_000_000_000
    sig = _routing_sig("+15551112222", exp, secret)
    assert _verify_routing_sig("+15551112222", str(exp), sig, secret, now=exp - 1) is True
    # expired
    assert _verify_routing_sig("+15551112222", str(exp), sig, secret, now=exp + 1) is False
    # tampered number
    assert _verify_routing_sig("+19990000000", str(exp), sig, secret, now=exp - 1) is False
    # wrong secret (attacker doesn't know the auth token)
    assert _verify_routing_sig("+15551112222", str(exp), sig, "guess", now=exp - 1) is False
    # missing sig / exp
    assert _verify_routing_sig("+15551112222", "", "", secret, now=exp - 1) is False


def test_resolve_stream_to_number_accepts_valid_signature() -> None:
    from tools.llm_proxy.voice_routes import _resolve_stream_to_number, _routing_sig

    to = "+15551112222"
    secret = "tok"
    exp = 2_000_000_000
    params = {"to": to, "exp": str(exp), "sig": _routing_sig(to, exp, secret)}
    clients = {to: _Ctx(secret)}
    assert _resolve_stream_to_number(params, clients, {}, "CA1", now=exp - 1) == to


def test_resolve_stream_to_number_rejects_forged_param_and_falls_back_to_bridge() -> None:
    """A forged 'to' (no/invalid sig) in production must NOT be trusted; the
    only way through is the signature-gated in-process bridge (same machine)."""
    from tools.llm_proxy.voice_routes import _resolve_stream_to_number

    to = "+15551112222"
    clients = {to: _Ctx("real-secret")}
    # Attacker connects straight to the WS with a forged 'to' and no valid sig.
    forged = {"to": to, "exp": "2000000000", "sig": "not-a-real-signature"}
    # No bridge entry for this call_sid → dropped (None).
    assert _resolve_stream_to_number(forged, clients, {}, "CA_forged", now=1) is None
    # A legitimate same-machine call still resolves via the bridge fallback.
    assert _resolve_stream_to_number({}, clients, {"CA_ok": to}, "CA_ok", now=1) == to


def test_resolve_stream_to_number_dev_mode_trusts_unsigned_param() -> None:
    """With no auth token configured (ALLOW_UNSIGNED dev), there's no secret to
    verify against, so the unsigned 'to' is trusted for local testing."""
    from tools.llm_proxy.voice_routes import _resolve_stream_to_number

    to = "+15551112222"
    clients = {to: _Ctx("")}  # empty auth_token = dev / unsigned mode
    assert _resolve_stream_to_number({"to": to}, clients, {}, "CA1", now=1) == to


def test_resolve_stream_to_number_none_when_absent_everywhere() -> None:
    from tools.llm_proxy.voice_routes import _resolve_stream_to_number

    assert _resolve_stream_to_number({}, {}, {}, "CA1", now=1) is None


def test_prune_stale_pending_calls() -> None:
    """Stale pending_calls entries (Twilio POST but no follow-up WS) must be
    swept on the next WS arrival so the dict can't grow without bound.

    Without this, a misconfigured ALLOW_UNSIGNED_TWILIO leaves the bridge dict
    open to unbounded inflation from spoofed POST /twilio/voice requests.
    """
    from tools.llm_proxy.voice_routes import _prune_stale_pending_calls

    now = 1_000.0
    pending = {
        "CA_stale": "+15551111111",
        "CA_fresh": "+15552222222",
    }
    started_at = {
        "CA_stale": now - 31.0,   # older than 30s TTL
        "CA_fresh": now - 5.0,    # well within TTL
    }
    pruned = _prune_stale_pending_calls(pending, started_at, now=now)
    assert pruned == 1
    assert "CA_stale" not in pending
    assert "CA_stale" not in started_at
    assert "CA_fresh" in pending
    assert "CA_fresh" in started_at


def test_per_client_factories_use_prefix(monkeypatch) -> None:
    """writer_from_env and notifier_from_env must read {PREFIX}_* vars, not bare globals (D-06)."""
    import inspect
    from tools.llm_proxy.sheets_writer import writer_from_env
    from tools.llm_proxy.sms_notifier import notifier_from_env

    # writer_from_env must have a prefix parameter.
    sig = inspect.signature(writer_from_env)
    assert "prefix" in sig.parameters, f"writer_from_env missing prefix param: {sig}"

    # notifier_from_env must have prefix + business identity params.
    sig2 = inspect.signature(notifier_from_env)
    assert "prefix" in sig2.parameters
    assert "business_name" in sig2.parameters
    assert "business_short_name" in sig2.parameters

    # Env routing: per-prefix vars must be isolated from bare globals.
    monkeypatch.setenv("CLEARSLOPE_GOOGLE_SHEET_ID", "sheet_cs")
    monkeypatch.setenv("ALLGREEN_GOOGLE_SHEET_ID", "sheet_ag")
    monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)

    assert os.environ.get("CLEARSLOPE_GOOGLE_SHEET_ID") == "sheet_cs"
    assert os.environ.get("ALLGREEN_GOOGLE_SHEET_ID") == "sheet_ag"
    assert os.environ.get("GOOGLE_SHEET_ID") is None  # bare global must be absent


# --- Host-header poisoning of the stream URL (audit S11) --------------------

def test_public_base_url_uses_env_and_ignores_host_header(monkeypatch) -> None:
    from types import SimpleNamespace
    from tools.llm_proxy.voice_routes import _public_base_url

    monkeypatch.setenv("PUBLIC_BASE_URL", "good.fly.dev")
    req = SimpleNamespace(headers={"host": "evil.example", "x-forwarded-host": "evil.example"})
    assert _public_base_url(req) == "wss://good.fly.dev"


def test_public_base_url_rejects_host_fallback_in_production(monkeypatch) -> None:
    """S11: with no PUBLIC_BASE_URL and not in dev, the attacker-controlled Host
    header must NOT be trusted to build the Media Stream URL (call-audio MITM)."""
    from types import SimpleNamespace

    from fastapi import HTTPException
    from tools.llm_proxy.voice_routes import _public_base_url

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("ALLOW_UNSIGNED_TWILIO", raising=False)
    req = SimpleNamespace(headers={"host": "evil.example"})
    with pytest.raises(HTTPException):
        _public_base_url(req)


def test_public_base_url_allows_host_fallback_in_dev(monkeypatch) -> None:
    from types import SimpleNamespace
    from tools.llm_proxy.voice_routes import _public_base_url

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
    req = SimpleNamespace(headers={"host": "localhost:8080"})
    assert _public_base_url(req) == "wss://localhost:8080"
