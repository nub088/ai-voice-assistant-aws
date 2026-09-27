"""Tests for tools/cost_per_call.py, the per-call operating-cost analyzer.

Hermetic: each test seeds a per-test tempfile SQLite DB and uses an inline rate
dict / temp rate file. Twilio is exercised through a fake client (no network).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tools.cost_per_call import (
    MEASURED,
    METERED,
    MODELED,
    CallRow,
    compute_call_cost,
    fetch_sms,
    fetch_voice_price,
    load_rates,
    main,
    read_calls,
    read_usage,
    render_json,
    render_text,
)

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
RATES = {
    "effective_date": "2026-06-03",
    "currency": "USD",
    "per_minute": {
        "twilio_voice": {"rate": 0.0085},
        "twilio_media_streams": {"rate": 0.0040},
        "deepgram_stt": {"low": 0.0043, "high": 0.0058},
        "cartesia_tts": {"low": 0.020, "high": 0.030},
        "anthropic_haiku": {"low": 0.005, "high": 0.010},
    },
    "per_message": {"twilio_sms": {"rate": 0.0079, "a2p_fee": 0.003}},
    "per_token": {
        "anthropic_haiku_4_5": {
            "input_per_mtok": 1.00,
            "output_per_mtok": 5.00,
            "cache_write_5m_per_mtok": 1.25,
            "cache_read_per_mtok": 0.10,
            "verified": False,
        }
    },
    "per_character": {"cartesia_sonic": {"rate_per_char": 0.0000303, "verified": False}},
}

_TRANSCRIPTS_DDL = """
CREATE TABLE IF NOT EXISTS transcripts (
    call_sid            TEXT PRIMARY KEY,
    client_id           TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    caller_number_last4 TEXT,
    duration_sec        REAL,
    redacted_text       TEXT NOT NULL,
    placeholder_counts  TEXT,
    residual_count      INTEGER NOT NULL DEFAULT 0,
    residual_labels     TEXT,
    quarantine          INTEGER NOT NULL DEFAULT 0
);
"""

_CALL_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS call_usage (
    call_sid                  TEXT PRIMARY KEY,
    client_id                 TEXT NOT NULL,
    created_at                TEXT NOT NULL,
    duration_sec              REAL,
    llm_model                 TEXT,
    llm_prompt_tokens         INTEGER NOT NULL DEFAULT 0,
    llm_completion_tokens     INTEGER NOT NULL DEFAULT 0,
    llm_cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    llm_cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    tts_model                 TEXT,
    tts_characters            INTEGER NOT NULL DEFAULT 0
);
"""


def _seed_transcript(db_path, *, call_sid, client_id="clearslope",
                     created_at="2026-06-02T12:00:00Z", duration_sec=300.0, last4=None):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_TRANSCRIPTS_DDL)
        conn.execute(
            "INSERT OR REPLACE INTO transcripts "
            "(call_sid, client_id, created_at, caller_number_last4, duration_sec, redacted_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (call_sid, client_id, created_at, last4, duration_sec, "x"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_usage(db_path, *, call_sid, client_id="clearslope", prompt=1000, completion=500,
                cache_read=8000, cache_creation=2000, chars=5000):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_CALL_USAGE_DDL)
        conn.execute(
            "INSERT OR REPLACE INTO call_usage "
            "(call_sid, client_id, created_at, duration_sec, llm_model, llm_prompt_tokens, "
            "llm_completion_tokens, llm_cache_read_tokens, llm_cache_creation_tokens, "
            "tts_model, tts_characters) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (call_sid, client_id, "2026-06-02T12:00:00Z", 300.0, "claude-haiku-4-5",
             prompt, completion, cache_read, cache_creation, "sonic", chars),
        )
        conn.commit()
    finally:
        conn.close()


class _FakeFetched:
    def __init__(self, price):
        self.price = price


class _FakeCalls:
    def __init__(self, price):
        self._price = price

    def __call__(self, _sid):
        return self

    def fetch(self):
        return _FakeFetched(self._price)


class _FakeMsg:
    def __init__(self, to, price, sid):
        self.to = to
        self.price = price
        self.sid = sid


class _FakeMessages:
    def __init__(self, msgs):
        self._msgs = msgs

    def list(self, **_kw):
        return self._msgs


class _FakeClient:
    def __init__(self, *, price=None, msgs=None):
        self.calls = _FakeCalls(price)
        self.messages = _FakeMessages(msgs or [])


# --------------------------------------------------------------------------- #
# Rates
# --------------------------------------------------------------------------- #
def test_load_rates_ok(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(RATES), encoding="utf-8")
    rates = load_rates(p)
    assert rates["effective_date"] == "2026-06-03"


def test_load_rates_missing_effective_date(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"per_minute": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="effective_date"):
        load_rates(p)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def test_read_calls_by_call_sid(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_transcript(db, call_sid="CA1", duration_sec=120.0, last4="1234")
    rows = read_calls(db, call_sid="CA1")
    assert len(rows) == 1
    assert rows[0].call_sid == "CA1"
    assert rows[0].duration_sec == 120.0
    assert rows[0].caller_number_last4 == "1234"


def test_read_calls_range_and_client_filter(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_transcript(db, call_sid="CA_old", created_at="2026-05-30T09:00:00Z")
    _seed_transcript(db, call_sid="CA_in", created_at="2026-06-02T09:00:00Z")
    _seed_transcript(db, call_sid="CA_other", created_at="2026-06-02T10:00:00Z", client_id="allgreen")
    _seed_transcript(db, call_sid="CA_late", created_at="2026-06-10T09:00:00Z")
    rows = read_calls(db, since="2026-06-01", until="2026-06-03", client_id="clearslope")
    assert [r.call_sid for r in rows] == ["CA_in"]


def test_read_calls_until_is_inclusive_whole_day(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_transcript(db, call_sid="CA_eod", created_at="2026-06-03T23:30:00Z")
    rows = read_calls(db, since="2026-06-03", until="2026-06-03")
    assert [r.call_sid for r in rows] == ["CA_eod"]


def test_read_usage_empty_when_table_absent(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_transcript(db, call_sid="CA1")
    assert read_usage(db, ["CA1"]) == {}


def test_read_usage_returns_rows_when_present(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_transcript(db, call_sid="CA1")
    _seed_usage(db, call_sid="CA1")
    usage = read_usage(db, ["CA1"])
    assert usage["CA1"]["llm_prompt_tokens"] == 1000
    assert usage["CA1"]["tts_characters"] == 5000


# --------------------------------------------------------------------------- #
# Cost model
# --------------------------------------------------------------------------- #
def _call(duration_sec=300.0, last4=None):
    return CallRow("CA1", "clearslope", "2026-06-02T12:00:00Z", duration_sec, last4)


def test_modeled_markers_when_no_usage_no_twilio():
    cost = compute_call_cost(_call(), RATES)
    by_provider = {ln.provider: ln for ln in cost.lines}
    assert by_provider["Twilio Voice"].marker == METERED        # modeled (no price)
    assert by_provider["Twilio Media Streams"].marker == METERED
    assert by_provider["Deepgram STT"].marker == METERED
    assert by_provider["Anthropic Haiku"].marker == MODELED
    assert by_provider["Cartesia TTS"].marker == MODELED


def test_modeled_totals_math():
    # 300s → mins_ceil=5, mins_frac=5.0
    cost = compute_call_cost(_call(300.0), RATES)
    # low: voice .0425 + media .020 + deepgram .0215 + anthropic .025 + cartesia .10
    assert cost.total_low == pytest.approx(0.209, abs=1e-9)
    # high: voice .0425 + media .020 + deepgram .029 + anthropic .05 + cartesia .15
    assert cost.total_high == pytest.approx(0.2915, abs=1e-9)


def test_minutes_round_up_for_twilio_lines():
    # 61s → ceil = 2 minutes for Twilio voice/media; deepgram uses fractional.
    cost = compute_call_cost(_call(61.0), RATES)
    voice = next(ln for ln in cost.lines if ln.provider == "Twilio Voice")
    assert voice.low == pytest.approx(0.0085 * 2)


def test_measured_voice_price_overrides_model():
    cost = compute_call_cost(_call(), RATES, voice_price=0.0123)
    voice = next(ln for ln in cost.lines if ln.provider == "Twilio Voice")
    assert voice.marker == MEASURED
    assert voice.low == voice.high == 0.0123


def test_measured_anthropic_and_cartesia_from_usage():
    usage = {
        "llm_prompt_tokens": 1000,
        "llm_completion_tokens": 500,
        "llm_cache_creation_tokens": 2000,
        "llm_cache_read_tokens": 8000,
        "tts_characters": 5000,
    }
    cost = compute_call_cost(_call(), RATES, usage=usage)
    anthropic = next(ln for ln in cost.lines if ln.provider == "Anthropic Haiku")
    cartesia = next(ln for ln in cost.lines if ln.provider == "Cartesia TTS")
    # 1000*1 + 500*5 + 2000*1.25 + 8000*0.10 = (1000+2500+2500+800)/1e6 = 0.0068
    assert anthropic.marker == MEASURED
    assert anthropic.low == pytest.approx(0.0068, abs=1e-9)
    assert "rate est." in anthropic.note  # verified:false
    # 5000 * 0.0000303 = 0.1515
    assert cartesia.marker == MEASURED
    assert cartesia.low == pytest.approx(0.1515, abs=1e-9)


def test_total_tokens_not_used_no_doublecount():
    # Even if a stray total_tokens leaks into usage, cost must ignore it.
    usage = {
        "llm_prompt_tokens": 1000,
        "llm_completion_tokens": 500,
        "llm_cache_creation_tokens": 2000,
        "llm_cache_read_tokens": 8000,
        "tts_characters": 0,
        "llm_total_tokens": 999999,  # must be ignored
    }
    cost = compute_call_cost(_call(), RATES, usage=usage)
    anthropic = next(ln for ln in cost.lines if ln.provider == "Anthropic Haiku")
    assert anthropic.low == pytest.approx(0.0068, abs=1e-9)


def test_sms_line_when_items_present():
    cost = compute_call_cost(_call(), RATES, sms_items=[("SM1", 0.0079), ("SM2", 0.0079)])
    sms = next(ln for ln in cost.lines if ln.provider == "Twilio SMS")
    assert sms.marker == METERED
    assert sms.low == pytest.approx(0.0158)
    assert "SM1" in sms.note and "SM2" in sms.note


# --------------------------------------------------------------------------- #
# Twilio (fake client)
# --------------------------------------------------------------------------- #
def test_fetch_voice_price_abs_value():
    assert fetch_voice_price(_FakeClient(price="-0.0085"), "CA1") == pytest.approx(0.0085)


def test_fetch_voice_price_none_when_unpriced():
    assert fetch_voice_price(_FakeClient(price=None), "CA1") is None
    assert fetch_voice_price(_FakeClient(price=""), "CA1") is None


def test_fetch_sms_filters_by_last4_and_sums():
    msgs = [
        _FakeMsg(to="+15550001234", price="-0.0079", sid="SM_match"),
        _FakeMsg(to="+15559999999", price="-0.0079", sid="SM_other"),
    ]
    items = fetch_sms(_FakeClient(msgs=msgs), _call(duration_sec=60.0, last4="1234"))
    assert [s for s, _ in items] == ["SM_match"]
    assert items[0][1] == pytest.approx(0.0079)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_render_text_has_legend_and_markers():
    cost = compute_call_cost(_call(), RATES)
    txt = render_text([cost], RATES)
    assert "Legend" in txt and MODELED in txt and "TOTAL" in txt
    assert "Anthropic Haiku" in txt


def test_render_json_shape():
    cost = compute_call_cost(_call(), RATES)
    payload = json.loads(render_json([cost], RATES))
    assert payload["calls"][0]["call_sid"] == "CA1"
    assert len(payload["calls"][0]["lines"]) == 5
    assert "grand_total_low" in payload


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #
def test_main_offline_single_call(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    _seed_transcript(db, call_sid="CA1", duration_sec=300.0)
    rates = tmp_path / "rates.json"
    rates.write_text(json.dumps(RATES), encoding="utf-8")
    rc = main(["--call-sid", "CA1", "--no-twilio", "--db", db, "--rates", str(rates)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Twilio Voice" in out and MODELED in out and "TOTAL" in out


def test_main_json_upgrades_with_usage(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    _seed_transcript(db, call_sid="CA1", duration_sec=300.0)
    _seed_usage(db, call_sid="CA1")
    rates = tmp_path / "rates.json"
    rates.write_text(json.dumps(RATES), encoding="utf-8")
    rc = main(["--call-sid", "CA1", "--no-twilio", "--json", "--db", db, "--rates", str(rates)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    lines = {ln["provider"]: ln for ln in payload["calls"][0]["lines"]}
    assert lines["Anthropic Haiku"]["marker"] == MEASURED
    assert lines["Cartesia TTS"]["marker"] == MEASURED


def test_main_no_calls_returns_1(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    _seed_transcript(db, call_sid="CA1")
    rates = tmp_path / "rates.json"
    rates.write_text(json.dumps(RATES), encoding="utf-8")
    rc = main(["--call-sid", "NOPE", "--no-twilio", "--db", db, "--rates", str(rates)])
    assert rc == 1


def test_main_requires_call_sid_or_range(tmp_path):
    rates = tmp_path / "rates.json"
    rates.write_text(json.dumps(RATES), encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--no-twilio", "--rates", str(rates)])
