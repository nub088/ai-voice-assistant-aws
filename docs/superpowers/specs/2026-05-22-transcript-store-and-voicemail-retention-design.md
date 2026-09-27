# Transcript Store + Voicemail Retention, Design

**Date:** 2026-05-22
**Status:** Approved, ready for implementation plan

## Goal

Replace the current "transcript = a log line that evaporates with Fly's log rotation" model with a durable, queryable store. Separately, shrink the window during which raw voicemail audio sits on Twilio's servers from 30 days to ~24 hours.

The transcript store is the main piece of work. The voicemail retention change is a one-line config tweak bundled in because it shares the privacy-posture theme.

## Non-goals

- No multi-machine support. SQLite on a single Fly volume is fine for current single-machine deploys; multi-machine is a Postgres migration deferred to a later spec.
- No transcript retention/expiry. Transcripts are already PII-redacted, so there is no liability rationale for deleting them.
- No new query/read API surface. Reading transcripts is a `sqlite3` CLI / `litecli` operator concern for now.
- No change to the Google Sheets booking pipeline.
- No change to the Deepgram live-call STT pipeline.

## The two pieces of work

### Piece A, Transcript SQLite store

#### Architecture

```
voice_pipeline.py::_persist_redacted_transcript
  ├─→ logger.info("REDACTED_TRANSCRIPT ...") [existing, kept as safety net]
  └─→ transcript_store.save(...)               [NEW]
            │
            └─→ SQLite file at /data/transcripts.db
                  (1 GB Fly volume mounted on the main app machine)
```

The redacted-log-line emission stays. It is one log call, costs nothing, and gives ops a grep-friendly backup during rollout. A follow-up commit may remove it once the SQLite store is proven.

#### New module: `tools/llm_proxy/transcript_store.py`

Responsibilities:
- Open and cache a SQLite connection (WAL mode, `synchronous=NORMAL`) on first call.
- Run `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` on first connection. No Alembic, no migration tool.
- Expose a single `save(...)` function that takes the same data the existing log line carries plus the metadata the call already has in scope (`client_id`, `duration_sec`, `caller_number`).
- Catch and log any `sqlite3.Error`; never raise into the call path. The call must finish successfully even if the DB is broken.
- Honor a configurable DB path via env (`TRANSCRIPT_DB_PATH`, default `/data/transcripts.db`). Tests override this with an in-memory or tempfile path.

The module must NOT depend on Pipecat, FastAPI, or any framework, pure stdlib `sqlite3` so it's trivial to unit-test and trivially portable.

#### Schema

```sql
CREATE TABLE IF NOT EXISTS transcripts (
    call_sid            TEXT PRIMARY KEY,
    client_id           TEXT NOT NULL,
    created_at          TEXT NOT NULL,        -- ISO 8601 UTC
    caller_number_last4 TEXT,                  -- "1401" not full number
    duration_sec        REAL,
    redacted_text       TEXT NOT NULL,
    placeholder_counts  TEXT,                  -- JSON: {"NAME": 3, "PHONE": 1}
    residual_count      INTEGER NOT NULL DEFAULT 0,
    residual_labels     TEXT,                  -- JSON array
    quarantine          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_transcripts_client_created
    ON transcripts(client_id, created_at);
```

Design notes:
- `call_sid` as PRIMARY KEY, Twilio guarantees uniqueness; an accidental double-write becomes a clean `INSERT OR IGNORE` no-op.
- `caller_number_last4` stores only the last 4 digits. The full number never enters the DB.
- `placeholder_counts` and `residual_labels` are JSON strings rather than separate tables. We accept that future ad-hoc analytics will need `json_extract()` queries; the alternative (normalizing them) is overkill at this scale.
- `quarantine` flag matches the existing `QUARANTINE_REDACTED_TRANSCRIPT` log prefix used when the redactor flagged residual PII.
- Schema is portable Postgres SQL. The eventual `pg_dump`-equivalent migration is a CSV export + `\copy` import; no schema rewrite.

#### Integration point

`voice_pipeline.py::_persist_redacted_transcript` already computes the redacted text, counts, and residuals at the end of every call. After the existing log line, add:

```python
try:
    transcript_store.save(
        call_sid=call_sid,
        client_id=client_ctx.config.client_id,
        redacted_text=result.redacted_text,
        placeholder_counts=result.counts,
        residual_labels=[r["label"] for r in result.residuals],
        residual_count=len(result.residuals),
        quarantine=bool(result.residuals),
        duration_sec=...,  # already computed earlier in the finally block
        caller_number_last4=caller_number[-4:] if caller_number else None,
    )
except Exception:
    logger.exception("call_sid=%s transcript_store.save failed", call_sid)
```

The `duration_sec` is computed in the surrounding `finally` block; the closure needs it threaded through. Implementation detail for the plan phase.

#### Fly volume

- Add a `[[mounts]]` section to `infra/fly/fly.toml` for the `app` process group:
  ```toml
  [[mounts]]
    source = "transcripts_data"
    destination = "/data"
    initial_size = "1gb"
  ```
- The `retention` process group does NOT mount this volume. Fly volumes are single-attach, and the retention machine doesn't need access, there's no transcript retention.
- 1 GB is wildly oversized (estimated growth ~2 MB/year) but is the smallest standard size and costs ~$0.15/month.
- Creating the volume itself is a one-time `fly volumes create transcripts_data --size 1 --region <existing-region>` performed during deploy; document this in RUNBOOK.

#### Tests

- `tests/test_transcript_store.py`, unit tests using an in-memory SQLite (`:memory:`):
  - Round-trip: `save()` then read back, verify all fields preserved.
  - Idempotency: calling `save()` twice with the same `call_sid` does not raise and produces one row (INSERT OR IGNORE).
  - JSON columns: `placeholder_counts` round-trips through JSON correctly.
  - Schema creation: the table is created on first connection.
- No integration test against a real Fly volume, covered by manual smoke after deploy.

#### "Transition later" note

Add to the module docstring of `transcript_store.py`:

> **Future migration.** SQLite was chosen because Fly volumes are effectively free at this scale and we run on a single machine. When we scale to multiple machines (auto-scaling, multi-region, or HA), this file becomes the bottleneck. Migrate to Fly Postgres at that point, the schema is intentionally portable, and the migration is `pg_dump`-shaped (CSV export → `\copy` import). Estimated cost at migration time: $2–5/month.

Plus a one-line entry in `RUNBOOK.md` under a new "Transcript store" heading pointing here.

### Piece B, Shorten voicemail audio retention to 24h

#### Change

In `tools/voicemail_retention.py`, change the default for `--audio-days` from `30` to `1`. The retention machine, already running daily via `infra/fly/fly.toml`'s `retention` process group with `--apply`, will start pruning recordings ~24-48 hours after creation instead of 30 days.

#### Why this is not a strict 24h delete

Fly's `schedule = "daily"` cron fires at ~00:00 UTC. A voicemail left at 23:30 UTC gets pruned at the next 00:00 (30 min later); a voicemail left at 00:30 UTC waits nearly 24 hours. So the practical retention window is 24–48 hours, not 24h flat. For the use case (owner gets paged via SMS within seconds and tapping the link is the expected behavior), this is acceptable.

#### Sheet rows

The Voicemails tab in the per-client Google Sheet keeps its existing 90-day row retention. Once the audio is gone the playback URL serves a 410 page (existing behavior), but the transcript row stays so historical lookups still work.

#### Docs

- Update `RUNBOOK.md` § Voicemail fallback → "Retention test" to reflect the new defaults (1-day audio, 90-day metadata).
- Update the docstring at the top of `voicemail_retention.py` to match.

#### Tests

No new tests. The change is a default-value tweak; existing tests for `voicemail_retention.py` (if any) cover the deletion logic.

## Risks and tradeoffs

1. **SMS playback URL window narrows from 30d to ~24h.** If the owner doesn't check their phone within a day, the audio link breaks (transcript is still in the Sheet). Considered acceptable, owners get a real-time page; this is the use case.
2. **Single-machine SQLite.** If we ever auto-scale or run multiple machines, this design breaks. Mitigation: documented migration path in the module docstring. Cost of being wrong: a half-day migration to Fly Postgres.
3. **No transcript expiry.** Anonymized data with no expiry could in theory still be re-identified if redaction has gaps. Mitigation: the `quarantine` column flags rows where the audit pass found residuals, so a future cleanup script can target only the high-risk subset.
4. **`/data` mount becomes a single-machine pin.** If we want to switch Fly regions or rebuild the machine, we need to remember the volume is part of the app's state now. Mitigation: documented in RUNBOOK.

## Success criteria

- A call lands and within seconds a row appears in `/data/transcripts.db`'s `transcripts` table with the correct `call_sid`, `client_id`, and redacted text.
- Killing the SQLite write (e.g., chmod the file readonly) does NOT cause calls to fail, the error is logged and the call completes.
- A voicemail older than ~25 hours is gone from Twilio after the next scheduled retention run.
- Unit tests for `transcript_store.py` pass with in-memory SQLite.
- `fly logs | grep REDACTED_TRANSCRIPT` still works (safety net intact).
