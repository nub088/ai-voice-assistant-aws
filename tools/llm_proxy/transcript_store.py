"""SQLite-backed durable store for redacted call transcripts.

The voice pipeline calls `save()` at end-of-call after PII redaction.
Writes are best-effort: any sqlite3 error is logged and swallowed so a
broken DB never breaks a live call.

The SQLite DB lives on a Fly volume at /data/transcripts.db. The AWS deployment
sets AWS_CALLS_TABLE to select the S3/DynamoDB backend instead. Override the
SQLite path via `TRANSCRIPT_DB_PATH` for local dev or tests.

# Future migration.
SQLite was chosen because Fly volumes are effectively free at this scale
and we run on a single machine. When we scale to multiple machines
(auto-scaling, multi-region, or HA), this file becomes the bottleneck.
Migrate to Fly Postgres at that point, the schema is intentionally
portable, and the migration is `pg_dump`-shaped (CSV export → `\\copy`
import). Estimated cost at migration: $2-5/month.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("transcript_store")

DEFAULT_DB_PATH = "/data/transcripts.db"

_SCHEMA_SQL = """
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
CREATE INDEX IF NOT EXISTS idx_transcripts_client_created
    ON transcripts(client_id, created_at);

-- Per-client running minute counter. Lives alongside transcripts so the
-- existing Fly volume mount (/data/transcripts.db) covers both. Survives
-- process restarts so a client's ceiling progress is not erased by deploys.
CREATE TABLE IF NOT EXISTS minute_counters (
    client_id   TEXT PRIMARY KEY,
    total_min   REAL NOT NULL DEFAULT 0.0,
    updated_at  TEXT NOT NULL
);

-- Per-call measured provider usage (LLM tokens + TTS characters), written at
-- end-of-call from the pipeline's metrics observer. Lets tools/cost_per_call.py
-- upgrade the Anthropic/Cartesia lines from modeled to measured. No
-- llm_total_tokens column: Pipecat's total_tokens excludes cache tokens, so it
-- is a misleading half-count the cost formula never uses.
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
CREATE INDEX IF NOT EXISTS idx_call_usage_client_created
    ON call_usage(client_id, created_at);
"""

# One connection per (thread, DB path), SC6. WAL mode (set below) allows
# concurrent readers alongside one writer; per-thread connections let SQLite
# arbitrate that instead of a Python global lock serializing every read
# against every write. Writer contention is handled by the 5 s busy timeout.
# Schema init still takes a process-wide lock so two threads racing on a
# fresh DB don't interleave the executescript.
_LOCAL = threading.local()
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()


def _get_connection(db_path: str) -> sqlite3.Connection:
    cache: dict[str, sqlite3.Connection] | None = getattr(_LOCAL, "conns", None)
    if cache is None:
        cache = _LOCAL.conns = {}
    conn = cache.get(db_path)
    if conn is not None:
        return conn
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    with _SCHEMA_LOCK:
        if db_path not in _SCHEMA_READY:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
            _SCHEMA_READY.add(db_path)
    cache[db_path] = conn
    return conn


def save(
    *,
    call_sid: str,
    client_id: str,
    redacted_text: str,
    placeholder_counts: dict[str, int],
    residual_labels: list[str],
    residual_count: int,
    quarantine: bool,
    duration_sec: float,
    caller_number_last4: str | None,
    db_path: str | None = None,
) -> None:
    """Persist a redacted transcript. Best-effort: errors are logged, not raised.

    Idempotent on `call_sid`: a duplicate insert is silently ignored so a
    retry path can't double-record.
    """
    if not db_path and os.environ.get("AWS_CALLS_TABLE"):
        from . import aws_store
        aws_store.save(
            call_sid=call_sid, client_id=client_id, redacted_text=redacted_text,
            placeholder_counts=placeholder_counts, residual_labels=residual_labels,
            residual_count=residual_count, quarantine=quarantine,
            duration_sec=duration_sec, caller_number_last4=caller_number_last4,
        )
        return
    path = db_path or os.environ.get("TRANSCRIPT_DB_PATH", DEFAULT_DB_PATH)
    try:
        conn = _get_connection(path)
        conn.execute(
            "INSERT OR IGNORE INTO transcripts ("
            "call_sid, client_id, created_at, caller_number_last4, "
            "duration_sec, redacted_text, placeholder_counts, "
            "residual_count, residual_labels, quarantine"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call_sid,
                client_id,
                datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                caller_number_last4,
                duration_sec,
                redacted_text,
                json.dumps(placeholder_counts, sort_keys=True),
                residual_count,
                json.dumps(residual_labels, sort_keys=True),
                1 if quarantine else 0,
            ),
        )
        conn.commit()
    except sqlite3.Error:
        logger.exception("call_sid=%s transcript_store.save failed", call_sid)
    except Exception:
        # Defensive: anything weirder than sqlite3.Error (e.g. OS errors on
        # the volume) also gets swallowed. The call must complete.
        logger.exception(
            "call_sid=%s transcript_store.save unexpected error", call_sid
        )


def save_minute_counter(
    *,
    client_id: str,
    total_minutes: float,
    db_path: str | None = None,
) -> None:
    """Persist a client's running minute counter. Best-effort.

    Called from the voice pipeline's end-of-call finally block after the
    in-memory counter is updated. Errors are logged but never raised, a
    failed counter write must not break a completed call.
    """
    if not db_path and os.environ.get("AWS_CALLS_TABLE"):
        from . import aws_store
        aws_store.save_minute_counter(client_id=client_id, total_minutes=total_minutes)
        return
    path = db_path or os.environ.get("TRANSCRIPT_DB_PATH", DEFAULT_DB_PATH)
    try:
        conn = _get_connection(path)
        conn.execute(
            "INSERT INTO minute_counters (client_id, total_min, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(client_id) DO UPDATE SET "
            "total_min = excluded.total_min, "
            "updated_at = excluded.updated_at",
            (
                client_id,
                float(total_minutes),
                datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            ),
        )
        conn.commit()
    except sqlite3.Error:
        logger.exception("client_id=%s save_minute_counter failed", client_id)
    except Exception:
        logger.exception(
            "client_id=%s save_minute_counter unexpected error", client_id
        )


def save_call_usage(
    *,
    call_sid: str,
    client_id: str,
    duration_sec: float,
    llm_model: str | None,
    llm_prompt_tokens: int,
    llm_completion_tokens: int,
    llm_cache_read_tokens: int,
    llm_cache_creation_tokens: int,
    tts_model: str | None,
    tts_characters: int,
    db_path: str | None = None,
) -> None:
    """Persist one call's measured LLM/TTS usage. Best-effort, idempotent.

    Called from the voice pipeline's end-of-call finally block with the totals
    accumulated by the metrics observer. `INSERT OR IGNORE` on the `call_sid`
    primary key makes a retry path a no-op. Errors are logged but never raised,     a failed usage write must not break a completed call.
    """
    if not db_path and os.environ.get("AWS_CALLS_TABLE"):
        from . import aws_store
        aws_store.save_call_usage(
            call_sid=call_sid, client_id=client_id, duration_sec=duration_sec,
            llm_model=llm_model, llm_prompt_tokens=llm_prompt_tokens,
            llm_completion_tokens=llm_completion_tokens,
            llm_cache_read_tokens=llm_cache_read_tokens,
            llm_cache_creation_tokens=llm_cache_creation_tokens,
            tts_model=tts_model, tts_characters=tts_characters,
        )
        return
    path = db_path or os.environ.get("TRANSCRIPT_DB_PATH", DEFAULT_DB_PATH)
    try:
        conn = _get_connection(path)
        conn.execute(
            "INSERT OR IGNORE INTO call_usage ("
            "call_sid, client_id, created_at, duration_sec, llm_model, "
            "llm_prompt_tokens, llm_completion_tokens, llm_cache_read_tokens, "
            "llm_cache_creation_tokens, tts_model, tts_characters"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call_sid,
                client_id,
                datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                duration_sec,
                llm_model,
                int(llm_prompt_tokens),
                int(llm_completion_tokens),
                int(llm_cache_read_tokens),
                int(llm_cache_creation_tokens),
                tts_model,
                int(tts_characters),
            ),
        )
        conn.commit()
    except sqlite3.Error:
        logger.exception("call_sid=%s save_call_usage failed", call_sid)
    except Exception:
        logger.exception("call_sid=%s save_call_usage unexpected error", call_sid)


def purge_expired(
    retention_days: int = 90,
    db_path: str | None = None,
    now: datetime | None = None,
) -> int:
    """Delete transcripts older than `retention_days`. Returns rows deleted (S3).

    Enforces the published "redacted transcripts kept up to 90 days then
    deleted" promise, without this the store grew forever. `created_at` is an
    ISO-8601 ...Z string, so a lexicographic `<` against a computed cutoff
    string compares correctly. `now` is injectable for tests.

    Errors are NOT swallowed, callers (the app's daily purge, an operator CLI)
    decide how to react; see the call sites for their try/except.
    """
    if not db_path and os.environ.get("AWS_CALLS_TABLE"):
        from . import aws_store
        return aws_store.purge_expired(retention_days=retention_days, now=now)
    path = db_path or os.environ.get("TRANSCRIPT_DB_PATH", DEFAULT_DB_PATH)
    cutoff_dt = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    cutoff = cutoff_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = _get_connection(path)
    cur = conn.execute(
        "DELETE FROM transcripts WHERE created_at < ?", (cutoff,)
    )
    conn.commit()
    return cur.rowcount


def delete_by_call_sid(call_sid: str, db_path: str | None = None) -> int:
    """Delete the transcript for one call. Returns rows deleted (DSAR erasure)."""
    if not db_path and os.environ.get("AWS_CALLS_TABLE"):
        from . import aws_store
        return aws_store.delete_by_call_sid(call_sid)
    path = db_path or os.environ.get("TRANSCRIPT_DB_PATH", DEFAULT_DB_PATH)
    conn = _get_connection(path)
    cur = conn.execute(
        "DELETE FROM transcripts WHERE call_sid = ?", (call_sid,)
    )
    conn.commit()
    return cur.rowcount


def delete_by_caller_last4(caller_number_last4: str, db_path: str | None = None) -> int:
    """Delete every transcript for a caller identified by last-4 (DSAR Art. 17).

    Returns rows deleted. Wired into tools/data_subject_request.py so a
    right-to-erasure request scrubs transcripts, not just the booking sheet.
    """
    if not db_path and os.environ.get("AWS_CALLS_TABLE"):
        from . import aws_store
        return aws_store.delete_by_caller_last4(caller_number_last4)
    path = db_path or os.environ.get("TRANSCRIPT_DB_PATH", DEFAULT_DB_PATH)
    conn = _get_connection(path)
    cur = conn.execute(
        "DELETE FROM transcripts WHERE caller_number_last4 = ?",
        (caller_number_last4,),
    )
    conn.commit()
    return cur.rowcount


def load_minute_counters(db_path: str | None = None) -> dict[str, float]:
    """Load all per-client minute counters from disk.

    Called once at app startup so the running totals survive process
    restarts. Returns an empty dict when the DB file doesn't exist yet
    (first deploy) or when any error occurs, the caller can safely treat
    the result as "default to zero".
    """
    if not db_path and os.environ.get("AWS_CALLS_TABLE"):
        from . import aws_store
        return aws_store.load_minute_counters()
    path = db_path or os.environ.get("TRANSCRIPT_DB_PATH", DEFAULT_DB_PATH)
    try:
        conn = _get_connection(path)
        rows = conn.execute(
            "SELECT client_id, total_min FROM minute_counters"
        ).fetchall()
        return {client_id: float(total_min) for client_id, total_min in rows}
    except sqlite3.Error:
        logger.exception("load_minute_counters failed")
        return {}
    except Exception:
        logger.exception("load_minute_counters unexpected error")
        return {}
