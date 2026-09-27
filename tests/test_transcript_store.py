"""Tests for the SQLite-backed transcript store.

These tests use a per-test tempfile DB path so each test gets a fresh
database. The store is sync stdlib sqlite3, no async / no fixtures
beyond the tmp_path pytest builtin.
"""
from __future__ import annotations

import json
import sqlite3

from tools.llm_proxy.transcript_store import (
    load_minute_counters,
    save,
    save_minute_counter,
)


def _read_rows(db_path: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT call_sid, client_id, caller_number_last4, duration_sec, "
            "redacted_text, placeholder_counts, residual_count, "
            "residual_labels, quarantine FROM transcripts"
        ).fetchall()
    finally:
        conn.close()


def test_save_round_trip(tmp_path) -> None:
    db_path = str(tmp_path / "transcripts.db")
    save(
        db_path=db_path,
        call_sid="CA_test_001",
        client_id="clearslope",
        redacted_text="Caller: [PHONE]\nSarah: hi",
        placeholder_counts={"PHONE": 1, "NAME": 1},
        residual_labels=[],
        residual_count=0,
        quarantine=False,
        duration_sec=42.5,
        caller_number_last4="1234",
    )
    rows = _read_rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "CA_test_001"
    assert row[1] == "clearslope"
    assert row[2] == "1234"
    assert row[3] == 42.5
    assert row[4] == "Caller: [PHONE]\nSarah: hi"
    assert json.loads(row[5]) == {"PHONE": 1, "NAME": 1}
    assert row[6] == 0
    assert json.loads(row[7]) == []
    assert row[8] == 0


def test_save_idempotent_on_duplicate_call_sid(tmp_path) -> None:
    db_path = str(tmp_path / "transcripts.db")
    save(
        db_path=db_path, call_sid="CA_dup", client_id="c",
        redacted_text="first", placeholder_counts={}, residual_labels=[],
        residual_count=0, quarantine=False, duration_sec=1.0,
        caller_number_last4=None,
    )
    save(
        db_path=db_path, call_sid="CA_dup", client_id="c",
        redacted_text="second", placeholder_counts={}, residual_labels=[],
        residual_count=0, quarantine=False, duration_sec=2.0,
        caller_number_last4=None,
    )
    rows = _read_rows(db_path)
    assert len(rows) == 1
    # INSERT OR IGNORE keeps the first write
    assert rows[0][4] == "first"


def test_save_with_quarantine_and_residuals(tmp_path) -> None:
    db_path = str(tmp_path / "transcripts.db")
    save(
        db_path=db_path, call_sid="CA_quar", client_id="c",
        redacted_text="leaked 555-1234", placeholder_counts={},
        residual_labels=["phone_like"], residual_count=1,
        quarantine=True, duration_sec=10.0, caller_number_last4=None,
    )
    rows = _read_rows(db_path)
    assert len(rows) == 1
    assert rows[0][6] == 1
    assert json.loads(rows[0][7]) == ["phone_like"]
    assert rows[0][8] == 1


def test_save_nullable_caller_number(tmp_path) -> None:
    db_path = str(tmp_path / "transcripts.db")
    save(
        db_path=db_path, call_sid="CA_null", client_id="c",
        redacted_text="t", placeholder_counts={}, residual_labels=[],
        residual_count=0, quarantine=False, duration_sec=0.0,
        caller_number_last4=None,
    )
    rows = _read_rows(db_path)
    assert rows[0][2] is None


def test_schema_is_created_on_first_save(tmp_path) -> None:
    db_path = str(tmp_path / "transcripts.db")
    # File doesn't exist yet
    save(
        db_path=db_path, call_sid="CA_first", client_id="c",
        redacted_text="t", placeholder_counts={}, residual_labels=[],
        residual_count=0, quarantine=False, duration_sec=0.0,
        caller_number_last4=None,
    )
    conn = sqlite3.connect(db_path)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        idx_names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_transcripts_client_created'"
        ).fetchall()]
    finally:
        conn.close()
    assert "transcripts" in names
    assert idx_names == ["idx_transcripts_client_created"]


# --- Minute-counter persistence -------------------------------------------------

def test_save_and_load_minute_counters(tmp_path) -> None:
    """Counters survive across process restarts by living in the same SQLite DB
    as transcripts. Without this, a Fly machine restart resets every client's
    minute ceiling to zero, a client at 95% of their cap would get a full
    fresh budget on each deploy.
    """
    db_path = str(tmp_path / "counters.db")
    save_minute_counter(client_id="alpha", total_minutes=42.5, db_path=db_path)
    save_minute_counter(client_id="beta", total_minutes=10.0, db_path=db_path)

    counters = load_minute_counters(db_path=db_path)
    assert counters == {"alpha": 42.5, "beta": 10.0}


def test_save_minute_counter_overwrites_previous_value(tmp_path) -> None:
    """End-of-call updates must replace, not append. INSERT OR REPLACE on the
    client_id primary key gives idempotent updates per client."""
    db_path = str(tmp_path / "counters.db")
    save_minute_counter(client_id="alpha", total_minutes=10.0, db_path=db_path)
    save_minute_counter(client_id="alpha", total_minutes=15.0, db_path=db_path)

    counters = load_minute_counters(db_path=db_path)
    assert counters == {"alpha": 15.0}


def test_load_minute_counters_empty_when_db_missing(tmp_path) -> None:
    """First-deploy case: no DB file yet. Must return {} so startup wiring
    can safely use it as a defaults source without crashing."""
    db_path = str(tmp_path / "never_created.db")
    counters = load_minute_counters(db_path=db_path)
    assert counters == {}


def test_save_minute_counter_does_not_affect_transcripts_table(tmp_path) -> None:
    """The two tables share a DB but are independent, saving a counter must
    not corrupt or write to the transcripts table."""
    db_path = str(tmp_path / "shared.db")
    save(
        db_path=db_path, call_sid="CA_a", client_id="alpha",
        redacted_text="t", placeholder_counts={}, residual_labels=[],
        residual_count=0, quarantine=False, duration_sec=1.0,
        caller_number_last4=None,
    )
    save_minute_counter(client_id="alpha", total_minutes=5.0, db_path=db_path)

    rows = _read_rows(db_path)
    assert len(rows) == 1  # transcript row untouched
    assert load_minute_counters(db_path=db_path) == {"alpha": 5.0}


# --- Per-call usage persistence (cost instrumentation, Phase 2) ------------

def test_save_call_usage_round_trip(tmp_path) -> None:
    from tools.llm_proxy.transcript_store import save_call_usage

    db_path = str(tmp_path / "usage.db")
    save_call_usage(
        db_path=db_path, call_sid="CA_u", client_id="clearslope", duration_sec=287.0,
        llm_model="claude-haiku-4-5", llm_prompt_tokens=14200, llm_completion_tokens=1850,
        llm_cache_read_tokens=96000, llm_cache_creation_tokens=5200,
        tts_model="sonic", tts_characters=4120,
    )
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT call_sid, client_id, duration_sec, llm_model, llm_prompt_tokens, "
            "llm_completion_tokens, llm_cache_read_tokens, llm_cache_creation_tokens, "
            "tts_model, tts_characters FROM call_usage"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "CA_u" and r[1] == "clearslope" and r[2] == 287.0
    assert r[3] == "claude-haiku-4-5" and r[4] == 14200 and r[5] == 1850
    assert r[6] == 96000 and r[7] == 5200 and r[8] == "sonic" and r[9] == 4120


def test_save_call_usage_idempotent_on_duplicate_call_sid(tmp_path) -> None:
    from tools.llm_proxy.transcript_store import save_call_usage

    db_path = str(tmp_path / "usage.db")
    common = dict(
        client_id="c", duration_sec=1.0, llm_model="m", llm_completion_tokens=2,
        llm_cache_read_tokens=3, llm_cache_creation_tokens=4, tts_model="t", tts_characters=5,
    )
    save_call_usage(db_path=db_path, call_sid="CA_dup", llm_prompt_tokens=1, **common)
    save_call_usage(db_path=db_path, call_sid="CA_dup", llm_prompt_tokens=999, **common)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT llm_prompt_tokens FROM call_usage").fetchall()
    finally:
        conn.close()
    assert rows == [(1,)]  # INSERT OR IGNORE keeps the first write


def test_save_call_usage_swallows_errors(tmp_path) -> None:
    """A broken DB must not raise into the call's end-of-call finally block."""
    from tools.llm_proxy.transcript_store import save_call_usage

    # Point the DB at an existing directory → sqlite cannot open it as a file.
    save_call_usage(
        db_path=str(tmp_path), call_sid="CA_x", client_id="c", duration_sec=0.0,
        llm_model=None, llm_prompt_tokens=0, llm_completion_tokens=0,
        llm_cache_read_tokens=0, llm_cache_creation_tokens=0, tts_model=None, tts_characters=0,
    )  # reaching here without an exception is the assertion


# --- Retention purge + DSAR erasure (audit finding S3) ---------------------

def test_purge_expired_deletes_old_and_keeps_recent(tmp_path) -> None:
    """No DELETE path existed, transcripts grew forever, breaking the
    published 90-day-deletion promise. purge_expired removes rows past the
    retention window (lexicographic compare on the ISO-8601 Z created_at)."""
    from datetime import datetime, timedelta, timezone

    from tools.llm_proxy.transcript_store import purge_expired

    db_path = str(tmp_path / "transcripts.db")
    for i in range(2):
        save(
            db_path=db_path, call_sid=f"CA{i}", client_id="c", redacted_text="x",
            placeholder_counts={}, residual_labels=[], residual_count=0,
            quarantine=False, duration_sec=1.0, caller_number_last4="1234",
        )
    now = datetime.now(timezone.utc)
    # Rows were just written → within the 90-day window → nothing purged.
    assert purge_expired(retention_days=90, db_path=db_path, now=now) == 0
    assert len(_read_rows(db_path)) == 2
    # Pretend it is 200 days later → both rows are now older than 90 days.
    assert purge_expired(retention_days=90, db_path=db_path, now=now + timedelta(days=200)) == 2
    assert _read_rows(db_path) == []


def test_delete_by_call_sid_removes_one_row(tmp_path) -> None:
    from tools.llm_proxy.transcript_store import delete_by_call_sid

    db_path = str(tmp_path / "transcripts.db")
    for sid in ("CA_keep", "CA_del"):
        save(
            db_path=db_path, call_sid=sid, client_id="c", redacted_text="x",
            placeholder_counts={}, residual_labels=[], residual_count=0,
            quarantine=False, duration_sec=1.0, caller_number_last4="1234",
        )
    assert delete_by_call_sid("CA_del", db_path=db_path) == 1
    assert [r[0] for r in _read_rows(db_path)] == ["CA_keep"]


def test_delete_by_caller_last4_erases_all_matching(tmp_path) -> None:
    """DSAR erasure: a requester identified by last-4 must have every matching
    transcript removed, not just the booking-sheet row."""
    from tools.llm_proxy.transcript_store import delete_by_caller_last4

    db_path = str(tmp_path / "transcripts.db")
    for sid, last4 in (("CA1", "1234"), ("CA2", "1234"), ("CA3", "9999")):
        save(
            db_path=db_path, call_sid=sid, client_id="c", redacted_text="x",
            placeholder_counts={}, residual_labels=[], residual_count=0,
            quarantine=False, duration_sec=1.0, caller_number_last4=last4,
        )
    assert delete_by_caller_last4("1234", db_path=db_path) == 2
    assert [r[0] for r in _read_rows(db_path)] == ["CA3"]


# --- per-thread connections (audit finding SC6) -------------------------------

def test_connections_are_per_thread(tmp_path):
    """SC6: each thread gets its own connection so WAL-mode concurrency is
    usable; a single shared connection + global lock serialized every
    read/write across all calls."""
    import threading
    from tools.llm_proxy import transcript_store as ts

    db = str(tmp_path / "t.db")
    # Hold strong references (not id()s), a dead thread's thread-local
    # connection can be GC'd and its memory address reused.
    conns = []

    def grab():
        conns.append(ts._get_connection(db))

    t1 = threading.Thread(target=grab)
    t2 = threading.Thread(target=grab)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert len(conns) == 2
    assert conns[0] is not conns[1]


def test_concurrent_saves_do_not_corrupt(tmp_path):
    """A burst of simultaneous end-of-call saves (post-P1 they run on worker
    threads) must all land without error."""
    import threading
    from tools.llm_proxy import transcript_store as ts

    db = str(tmp_path / "t.db")
    errors: list[Exception] = []

    def write(i: int) -> None:
        try:
            ts.save_minute_counter(
                client_id=f"c{i % 3}", total_minutes=float(i), db_path=db
            )
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    counters = ts.load_minute_counters(db_path=db)
    assert set(counters) == {"c0", "c1", "c2"}
