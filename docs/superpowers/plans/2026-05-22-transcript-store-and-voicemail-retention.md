# Transcript Store + Voicemail Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist redacted call transcripts in SQLite on a Fly volume (replacing log-line-only storage), and shrink Twilio voicemail audio retention from 30 days to ~24 hours.

**Architecture:** New `tools/llm_proxy/transcript_store.py` module exposes a single `save()` function backed by SQLite at `/data/transcripts.db` on a 1 GB Fly volume mounted on the receptionist app machine. `voice_pipeline.py::_persist_redacted_transcript` calls `save()` after the existing log line as a best-effort write (failures log but never raise into the call path). Separately, `tools/voicemail_retention.py` default for `--audio-days` drops from 30 to 1.

**Tech Stack:** Python 3.11+, stdlib `sqlite3` (WAL mode), pytest, Fly.io volumes (single-attach), Twilio Recordings API.

**Spec:** [`docs/superpowers/specs/2026-05-22-transcript-store-and-voicemail-retention-design.md`](../specs/2026-05-22-transcript-store-and-voicemail-retention-design.md)

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `tools/llm_proxy/transcript_store.py` | Create | SQLite store with `save()` + `_ensure_schema()` + connection cache |
| `tests/test_transcript_store.py` | Create | Unit tests using tempfile SQLite |
| `tools/llm_proxy/voice_pipeline.py` | Modify (lines 285-334) | Thread `client_id` / `caller_number` / `duration_sec` into `_persist_redacted_transcript`; call `transcript_store.save()` |
| `tests/test_voice_pipeline.py` | Modify (append) | Integration test verifying the save call wiring |
| `infra/fly/fly.toml` | Modify | Add `[[mounts]]` section for the `app` process group |
| `tools/voicemail_retention.py` | Modify (lines 8-9, 119) | Change `--audio-days` default 30 → 1; update docstring |
| `tests/test_voicemail_retention.py` | Modify (append) | Test that default is 1 |
| `RUNBOOK.md` | Modify | Add "Transcript store" section; update voicemail retention defaults |

---

## Task 1: Create `transcript_store.py` module

**Files:**
- Create: `tools/llm_proxy/transcript_store.py`
- Test: `tests/test_transcript_store.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_transcript_store.py`:

```python
"""Tests for the SQLite-backed transcript store.

These tests use a per-test tempfile DB path so each test gets a fresh
database. The store is sync stdlib sqlite3, no async / no fixtures
beyond the tmp_path pytest builtin.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tools.llm_proxy.transcript_store import save


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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
.venv/bin/pytest tests/test_transcript_store.py -v
```

Expected: `ModuleNotFoundError: No module named 'tools.llm_proxy.transcript_store'`

- [ ] **Step 3: Write the module**

Create `tools/llm_proxy/transcript_store.py`:

```python
"""SQLite-backed durable store for redacted call transcripts.

The voice pipeline calls `save()` at end-of-call after PII redaction.
Writes are best-effort: any sqlite3 error is logged and swallowed so a
broken DB never breaks a live call.

The DB lives on a Fly volume at /data/transcripts.db. Override via the
`TRANSCRIPT_DB_PATH` env var for local dev or tests.

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
from datetime import datetime, timezone
from typing import Any

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
"""

# Cache one connection per DB path. SQLite is single-writer; concurrent
# saves serialize through this lock, which is fine at our call rate.
_CONNECTIONS: dict[str, sqlite3.Connection] = {}
_LOCK = threading.Lock()


def _get_connection(db_path: str) -> sqlite3.Connection:
    with _LOCK:
        conn = _CONNECTIONS.get(db_path)
        if conn is not None:
            return conn
        conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        _CONNECTIONS[db_path] = conn
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
    path = db_path or os.environ.get("TRANSCRIPT_DB_PATH", DEFAULT_DB_PATH)
    try:
        conn = _get_connection(path)
        with _LOCK:
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
.venv/bin/pytest tests/test_transcript_store.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add tools/llm_proxy/transcript_store.py tests/test_transcript_store.py
git commit -m "$(cat <<'EOF'
feat(transcripts): add SQLite-backed transcript store

New tools/llm_proxy/transcript_store.py exposes a single best-effort
save() function backed by SQLite (WAL mode, INSERT OR IGNORE for
idempotency on call_sid). Schema is Postgres-portable so the eventual
Fly Postgres migration is a CSV export, not a rewrite.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Wire `transcript_store.save()` into the voice pipeline

**Files:**
- Modify: `tools/llm_proxy/voice_pipeline.py` (lines 285-334 and the call site at ~538)
- Test: `tests/test_voice_pipeline.py` (append)

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_voice_pipeline.py`:

```python
def test_persist_redacted_transcript_writes_to_store(monkeypatch, tmp_path) -> None:
    """End-of-call persistence should call transcript_store.save with the
    redacted text and metadata threaded from run_voice_session's scope.
    """
    from tools.llm_proxy import transcript_store, voice_pipeline
    from tools.redact_pii import RedactionResult

    # Avoid the expensive Presidio init, substitute a fake redactor.
    class _FakeRedactor:
        def redact_text(self, text: str) -> RedactionResult:
            return RedactionResult(
                redacted_text="[redacted]: " + text,
                counts={"NAME": 1},
                residuals=[],
            )

    monkeypatch.setattr(voice_pipeline, "_REDACTOR", _FakeRedactor())
    monkeypatch.setattr(voice_pipeline, "_REDACTOR_INITIALIZED", True)

    captured: dict[str, object] = {}

    def _fake_save(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(transcript_store, "save", _fake_save)

    voice_pipeline._persist_redacted_transcript(
        call_sid="CA_wired",
        messages=[
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
        retain=True,
        assistant_label="Sarah",
        client_id="clearslope",
        duration_sec=12.3,
        caller_number_last4="5678",
    )

    assert captured["call_sid"] == "CA_wired"
    assert captured["client_id"] == "clearslope"
    assert captured["duration_sec"] == 12.3
    assert captured["caller_number_last4"] == "5678"
    assert "hello" in captured["redacted_text"]
    assert captured["placeholder_counts"] == {"NAME": 1}
    assert captured["residual_count"] == 0
    assert captured["quarantine"] is False


def test_persist_redacted_transcript_skips_when_not_retained(monkeypatch) -> None:
    """retain=False short-circuits before any save call."""
    from tools.llm_proxy import transcript_store, voice_pipeline

    called = False

    def _fake_save(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(transcript_store, "save", _fake_save)

    voice_pipeline._persist_redacted_transcript(
        call_sid="CA_optout",
        messages=[{"role": "user", "content": "no thanks"}],
        retain=False,
        assistant_label="Sarah",
        client_id="clearslope",
        duration_sec=1.0,
        caller_number_last4=None,
    )
    assert called is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
.venv/bin/pytest tests/test_voice_pipeline.py::test_persist_redacted_transcript_writes_to_store -v
```

Expected: `TypeError: _persist_redacted_transcript() got an unexpected keyword argument 'client_id'` (or similar, the function doesn't accept the new params yet).

- [ ] **Step 3: Extend the function signature and add the save call**

Edit `tools/llm_proxy/voice_pipeline.py` lines 285-334. Replace the existing `_persist_redacted_transcript` function with:

```python
def _persist_redacted_transcript(
    *,
    call_sid: str,
    messages: list[dict[str, Any]],
    retain: bool,
    assistant_label: str = "Assistant",
    client_id: str,
    duration_sec: float,
    caller_number_last4: str | None,
) -> None:
    """Emit the redacted transcript for this call as a log line + SQLite row.

    Two persistence surfaces:
      1. Structured log line (REDACTED_TRANSCRIPT prefix), grep-friendly,
         kept as a safety net during the SQLite rollout.
      2. Durable SQLite row via transcript_store.save(), the actual store.

    When `retain=False`, both are skipped and an opt-out audit line is
    logged.
    """
    if not retain:
        logger.info("call_sid=%s transcript_retain=false; skipping persistence", call_sid)
        return

    raw = _extract_transcript_text(messages, assistant_label=assistant_label)
    if not raw:
        logger.info("call_sid=%s transcript empty; nothing to persist", call_sid)
        return

    redactor = _get_redactor()
    if redactor is None:
        logger.warning(
            "call_sid=%s redactor unavailable; transcript not persisted "
            "(check REDACTION_HMAC_KEY env)",
            call_sid,
        )
        return

    try:
        result = redactor.redact_text(raw)
    except Exception:
        logger.exception("call_sid=%s redaction failed; transcript not persisted", call_sid)
        return

    quarantine_prefix = "QUARANTINE_" if result.residuals else ""
    # Single structured line, keep it grep-friendly.
    logger.info(
        "%sREDACTED_TRANSCRIPT call_sid=%s residuals=%d counts=%s content=%r",
        quarantine_prefix,
        call_sid,
        len(result.residuals),
        result.counts,
        result.redacted_text,
    )

    # Durable store, best-effort, never raises into the call path.
    from tools.llm_proxy import transcript_store
    transcript_store.save(
        call_sid=call_sid,
        client_id=client_id,
        redacted_text=result.redacted_text,
        placeholder_counts=dict(result.counts),
        residual_labels=[r["label"] for r in result.residuals],
        residual_count=len(result.residuals),
        quarantine=bool(result.residuals),
        duration_sec=duration_sec,
        caller_number_last4=caller_number_last4,
    )
```

- [ ] **Step 4: Update the call site in `run_voice_session`**

In `tools/llm_proxy/voice_pipeline.py`, find the call to `_persist_redacted_transcript` at the bottom of `run_voice_session` (around lines 538-543). Replace it with:

```python
        _persist_redacted_transcript(
            call_sid=call_sid,
            messages=list(context.messages),
            retain=retain,
            assistant_label=client_ctx.config.business.ai_name,
            client_id=client_ctx.config.client_id,
            duration_sec=elapsed_sec,
            caller_number_last4=caller_number[-4:] if caller_number else None,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
.venv/bin/pytest tests/test_voice_pipeline.py -v
```

Expected: all tests pass, including the two new ones.

- [ ] **Step 6: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add tools/llm_proxy/voice_pipeline.py tests/test_voice_pipeline.py
git commit -m "$(cat <<'EOF'
feat(transcripts): wire transcript_store.save into end-of-call flow

_persist_redacted_transcript now takes client_id, duration_sec, and
caller_number_last4 (threaded from run_voice_session's scope) and
writes a durable row to SQLite in addition to the existing log line.
The log line stays as a safety net during rollout.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add Fly volume mount to fly.toml

**Files:**
- Modify: `infra/fly/fly.toml`

- [ ] **Step 1: Add the mount section**

Edit `infra/fly/fly.toml`. After the `[http_service.concurrency]` block and before the first `[[vm]]` block, add:

```toml
[[mounts]]
  source = "transcripts_data"
  destination = "/data"
  initial_size = "1gb"
  processes = ["app"]
```

The `processes = ["app"]` restricts the volume to the receptionist app process group. The `retention` process group runs on a separate scheduled machine and must NOT mount this volume, Fly volumes are single-attach, and the retention machine doesn't need transcript access.

- [ ] **Step 2: Document the volume-create command in RUNBOOK**

This is a one-time `flyctl` command run before the next deploy. It's covered in Task 5 (RUNBOOK update). No deploy happens as part of this task, the spec doesn't include deploying.

- [ ] **Step 3: Verify the toml parses**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
python -c "import tomllib; tomllib.loads(open('infra/fly/fly.toml').read()); print('ok')"
```

Expected output: `ok`

- [ ] **Step 4: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add infra/fly/fly.toml
git commit -m "$(cat <<'EOF'
infra(fly): mount transcripts_data volume at /data on app process

1 GB volume backs the new SQLite transcript store. Restricted to the app
process group; the retention scheduled machine does not mount it (Fly
volumes are single-attach and retention has nothing to clean up there).

Volume must be created out-of-band before deploy:
  fly volumes create transcripts_data --size 1 --region sjc \\
    --app clearslope-receptionist

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Shorten voicemail audio retention default to 1 day

**Files:**
- Modify: `tools/voicemail_retention.py` (lines 8 and 119)
- Test: `tests/test_voicemail_retention.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_voicemail_retention.py`:

```python
def test_audio_days_default_is_one_in_real_parser(monkeypatch, capsys) -> None:
    """Audio retention default is 1 day, not the historical 30 days.

    Drives the real argparse parser via --help and inspects the rendered
    default, so this test fails if anyone reverts the constant in
    voicemail_retention.py. The Fly scheduled retention machine runs
    daily, so in practice recordings get pruned 24-48 hours after
    creation. Design decision is in
    docs/superpowers/specs/2026-05-22-transcript-store-and-voicemail-retention-design.md
    """
    import sys

    from tools import voicemail_retention

    monkeypatch.setattr(sys, "argv", ["voicemail_retention", "--help"])
    with pytest.raises(SystemExit):
        voicemail_retention.main()
    out = capsys.readouterr().out
    # argparse renders defaults like: --audio-days AUDIO_DAYS  ... (default: 1)
    assert "(default: 1)" in out
    # Sanity: metadata default unchanged
    assert "(default: 90)" in out
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
.venv/bin/pytest tests/test_voicemail_retention.py::test_audio_days_default_is_one_in_real_parser -v
```

Expected: AssertionError, current default is 30.

- [ ] **Step 3: Update the default in voicemail_retention.py**

Edit `tools/voicemail_retention.py` line 119:

Change from:
```python
    p.add_argument("--audio-days", type=int, default=30)
```

To:
```python
    p.add_argument("--audio-days", type=int, default=1)
```

Also update the module docstring at line 8. Change from:
```python
  1. Twilio recordings older than --audio-days (default 30).
```

To:
```python
  1. Twilio recordings older than --audio-days (default 1; daily cron
     yields a practical 24-48h retention window).
```

- [ ] **Step 4: Run all retention tests to verify**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
.venv/bin/pytest tests/test_voicemail_retention.py -v
```

Expected: all tests pass, including the new one. Existing tests that explicitly pass `max_age_days=30` are unaffected (the default change doesn't break them).

- [ ] **Step 5: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add tools/voicemail_retention.py tests/test_voicemail_retention.py
git commit -m "$(cat <<'EOF'
fix(retention): shrink voicemail audio retention from 30d to ~24h

Default --audio-days drops from 30 to 1. The Fly scheduled retention
machine still runs daily, so practical retention is 24-48h depending on
when a voicemail lands relative to the 00:00 UTC cron. Sheet rows in the
Voicemails tab keep their 90-day retention; the SMS playback URL becomes
a 410 page once the audio is pruned (existing behavior).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Update RUNBOOK.md

**Files:**
- Modify: `RUNBOOK.md`

- [ ] **Step 1: Add the "Transcript store" section**

Edit `RUNBOOK.md`. Insert the following section after the existing "Voicemail fallback" section and before "Routine maintenance":

```markdown
---

## Transcript store

Redacted call transcripts are persisted to SQLite at `/data/transcripts.db`
on a Fly volume mounted on the receptionist app machine. This is in
addition to the `REDACTED_TRANSCRIPT` log line (which remains as a
safety-net surface during rollout).

### One-time volume create

Before the first deploy that includes the new `[[mounts]]` block in
`infra/fly/fly.toml`, create the volume:

```bash
fly volumes create transcripts_data --size 1 --region sjc \
  --app clearslope-receptionist
```

Skipping this step makes the deploy fail with "volume not found." Doing
it twice fails with "volume already exists", safe to ignore.

### Inspecting transcripts

The DB is a regular SQLite file. SSH into the machine and use the
standard CLI:

```bash
fly ssh console --app clearslope-receptionist
# inside the machine:
sqlite3 /data/transcripts.db
sqlite> SELECT call_sid, client_id, created_at FROM transcripts
        ORDER BY created_at DESC LIMIT 20;
```

For ad-hoc analysis from a laptop, copy the file off:

```bash
fly ssh sftp shell --app clearslope-receptionist
sftp> get /data/transcripts.db /tmp/transcripts.db
sftp> exit
sqlite3 /tmp/transcripts.db
```

### When SQLite isn't enough

The DB is single-machine: if we ever auto-scale or run multiple
receptionist machines, the volume becomes a single point of bottleneck
and SQLite serializes writes through one process. At that point migrate
to Fly Postgres, the schema is Postgres-portable, so the migration is a
CSV export + `\copy` import:

```bash
sqlite3 /data/transcripts.db \
  -cmd ".headers on" -cmd ".mode csv" \
  -cmd ".output transcripts.csv" \
  -cmd "SELECT * FROM transcripts;" \
  -cmd ".quit"
# then on the Postgres side:
\copy transcripts FROM 'transcripts.csv' WITH (FORMAT csv, HEADER true);
```

Estimated cost at migration: $2-5/month for the smallest Fly Postgres.

### No retention

Transcripts are PII-redacted before they hit the DB, so there's no
liability rationale for retention. The file grows ~2 MB/year at current
call volume. If it ever becomes a problem, add a retention pass, but
that's not currently scheduled.
```

- [ ] **Step 2: Update the voicemail retention defaults in RUNBOOK**

In `RUNBOOK.md`, find the "Retention test" subsection under "Voicemail fallback" (around line 297). The dry-run command block currently reads:

```bash
# Dry-run (default)
python -m tools.voicemail_retention --prefix ALLGREEN
```

After that block, the existing prose mentions "force-prune for testing (1-day audio, 1-day metadata)", that's an explicit override, fine to leave alone. But the headline default has changed. Add this paragraph immediately after the "Dry-run (default)" code block and before the "Apply for real" block:

```markdown
**Defaults**: `--audio-days` is now `1` (was 30) and `--metadata-days`
is `90`. The Fly retention machine runs daily, so Twilio recordings get
pruned 24-48 hours after creation. Sheet rows in the Voicemails tab are
pruned at 90 days.
```

- [ ] **Step 3: Verify the markdown is well-formed**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
# Sanity check: no broken section headers
grep -c "^## " RUNBOOK.md
```

Expected: a positive integer (the count of `##` headings), and no errors.

Visually scan the new section for trailing whitespace or unclosed code fences.

- [ ] **Step 4: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add RUNBOOK.md
git commit -m "$(cat <<'EOF'
docs(runbook): document transcript SQLite store + new retention defaults

Adds a "Transcript store" ops section covering one-time volume create,
inspection workflow, the Postgres migration path, and why there's no
retention. Also updates the voicemail-retention "Retention test"
subsection to reflect the new --audio-days=1 default.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Verification

After all 5 tasks land, run the full test suite to catch any regressions:

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
.venv/bin/pytest tests/ -v
```

Expected: all tests pass (including the existing suite plus the new transcript_store, voice_pipeline integration, and voicemail_retention default tests).

### Manual smoke after deploy (out of scope for this plan)

These steps are documented here for completeness but happen on the next deploy, not as part of this plan:

1. `fly volumes create transcripts_data --size 1 --region sjc --app clearslope-receptionist`
2. `flyctl deploy -c infra/fly/fly.toml --app clearslope-receptionist`
3. Make a test call; verify a row appears in `/data/transcripts.db` (via `fly ssh console` → `sqlite3`).
4. Leave a test voicemail; verify the recording is gone from Twilio after the next daily retention pass.
