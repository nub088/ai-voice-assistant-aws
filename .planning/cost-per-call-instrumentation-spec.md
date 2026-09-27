# Cost-Per-Call Instrumentation, Implementation Spec

Status: **DRAFT for build** · Author: agent · Date: 2026-06-03
Scope confirmed by user: (1) Twilio-API spine, (2) `config/cost_rates.json` as rate home, (3) Phase 2 touches `voice_pipeline.py`.

This spec is grounded in the current codebase (file:line citations throughout) and resolves
the three items the design left open: the metrics-observer behavior, the exact Pipecat field
names, and the SQLite write pattern to mirror.

---

## 0. One-paragraph summary

Build `tools/cost_per_call.py`, a **read-only, $0** tool that reports the all-in cost of a
call (or a date range of calls), rendering each provider line with a provenance marker
(✅ measured · 〜 metered/proxied · ⚙ modeled). **Phase 1** ships entirely from data we already
have (local `transcripts.duration_sec` + per-minute rates in a new `config/cost_rates.json`),
optionally upgrading the Twilio Voice and SMS lines to ✅ when Twilio creds are present.
**Phase 2** is an additive, async, hot-path-safe write: extend the existing call-metrics observer
to accumulate the LLM-token and TTS-character usage Pipecat *already emits*, flush one row to a
new `call_usage` SQLite table at call-end (mirroring the `minute_counters` write), and have
`cost_per_call.py` upgrade the Anthropic + Cartesia lines from ⚙ modeled to ✅ measured whenever a
`call_usage` row exists.

---

## 1. Goals & non-goals

**Goals**
- Post-hoc, per-call and per-period cost attribution across all six providers.
- Honest provenance: never let an estimate read as a measurement. Every line is tagged ✅/〜/⚙.
- $0 and read-only for Phase 1 (adds a tool + a rate file; touches no running code).
- Phase 2 adds *measured* token/char usage with **zero added latency** on the audio hot path.
- Single hand-maintained rate file with an `effective_date`; no live rate scraping.

**Non-goals (YAGNI, explicitly out)**
- Cost-per-booking / margin (the `call_sid`→Sheets linkage enables it later; separate tool).
- Real-time / in-call cost display. This is post-hoc only.
- Auto-updating rates from provider pricing pages.
- Per-client rate overrides (rates are global; `config/clients/*.json` stays cost-free).

---

## 2. Grounded findings (verified against the code)

### 2.1 Metrics observer, confirmed, with a bonus
- `TTFBLogObserver(BaseObserver)` at `tools/llm_proxy/voice_pipeline.py:121-144`.
  `on_push_frame` filters to `MetricsFrame`, dedups by `frame.id` via `self._seen`, then calls
  `_ttfb_log_lines` (`:104-118`), which keeps **only** `TTFBMetricsData` (`:113`) and drops
  everything else.
- **`enable_usage_metrics=True`** at `voice_pipeline.py:99` (inside `_build_pipeline_params`),   so `LLMUsageMetricsData` and `TTSUsageMetricsData` frames **are already being produced** and are
  reaching the observer; they're just discarded at the `isinstance` gate. ⇒ Phase 2 needs no
  TextFrame-counting fallback.
- Observer attached at `voice_pipeline.py:471`: `observers=[TTFBLogObserver(call_sid=call_sid)]`.

### 2.2 Pipecat metrics field names, pinned (pipecat-ai==1.2.1, `requirements.txt:24`)
> **Interpreter pin:** the app runtime is the project **`.venv` (Python 3.14)**, its startup
> banner prints "Pipecat 1.2.1" and `.venv/bin/python -c "import pipecat; print(pipecat.__version__)"`
> returns `1.2.1`. A bare system `python3` may resolve a *different/older* pipecat (a verification
> agent saw `1.1.0` that way). Always check against `.venv/bin/python`. Every field below was read
> from the `.venv` install.

From `pipecat/metrics/metrics.py`:
```python
class LLMTokenUsage(BaseModel):           # :49-65
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None

class LLMUsageMetricsData(MetricsData):    # :68-75
    value: LLMTokenUsage

class TTSUsageMetricsData(MetricsData):    # :78-85
    value: int                             # character count == len(text)
```
`MetricsFrame.data: list[MetricsData]` (`pipecat/frames/frames.py:1096`). `MetricsData` carries
`processor` and `model` fields (used today by `_ttfb_log_lines` as `m.processor`, `m.model`).
> Note the field naming: Pipecat uses **`prompt_tokens` / `completion_tokens`**, NOT
> `input_tokens` / `output_tokens`. `prompt_tokens` is the **non-cached** input count; cache
> reads/writes are reported *separately* (`cache_read_input_tokens`, `cache_creation_input_tokens`)
> and are NOT included in `prompt_tokens` (matches Anthropic `usage.input_tokens` semantics,
> per `pipecat/services/anthropic/llm.py:443-444` for `prompt_tokens`, `:460-477` for cache).
> This matters for the cost math (§6).
>
> **`total_tokens` = `prompt_tokens + completion_tokens` only** (metrics.py:55 docstring;
> `anthropic/llm.py:555`), it **excludes** cache tokens. We therefore do NOT store or use it for
> cost (§6.1, §6.5); summing it with the cache lines would double-count. `reasoning_tokens` exists
> on the model but is N/A for Haiku in this pipeline; not stored.

### 2.3 SQLite persistence, the pattern to mirror
- Owner module: `tools/llm_proxy/transcript_store.py`. DB at `/data/transcripts.db`
  (override `TRANSCRIPT_DB_PATH`). Tables: `transcripts`, `minute_counters` (`:31-55`).
- Sync stdlib `sqlite3` + process-wide `threading.Lock` (`_LOCK`) + per-path connection cache
  (`_get_connection`, `:63-74`), `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`.
- Schema applied via `conn.executescript(_SCHEMA_SQL)` on every fresh connection, adding a
  `CREATE TABLE IF NOT EXISTS call_usage` to `_SCHEMA_SQL` is safe and self-migrating.
- Writes are **best-effort**: `sqlite3.Error` and bare `Exception` are logged and swallowed so a
  broken DB never breaks a live call (`save`, `:119-126`; `save_minute_counter`, `:160-165`).
- Hot path never blocks: the end-of-call `finally` block offloads the sync write via
  `loop.run_in_executor(None, functools.partial(transcript_store.save_minute_counter, ...))`
  (`voice_pipeline.py:570-578`).
- `transcripts.duration_sec` (REAL) is stored per `call_sid` (`transcript_store.py:37`,
  written from `voice_pipeline.py:597`), **this is the local duration source for Phase 1's
  modeled lines, no API call required.**

### 2.4 Call lifecycle, where to accumulate & flush
- `call_sid` extracted at `/twilio/voice` POST (`voice_routes.py:233`), bridged via
  `app.state.pending_calls`, recovered from the Media Streams `start` event
  (`voice_routes.py:336-346`), passed into `run_voice_session(..., call_sid=...)`
  (`voice_pipeline.py:358`).
- Call-end teardown = the `finally` block at `voice_pipeline.py:551-599` (runs on normal end and
  on pipeline error). This is exactly where `save_minute_counter` and the redacted-transcript
  write already happen, the `call_usage` flush goes here too.
- The observer instance lives for the whole call and already holds `call_sid` ⇒ accumulate there;
  read its totals in the `finally` block. (No new closure/state dict needed, simpler than the
  `recording_state` pattern at `function_tools.py:269`.)

### 2.5 Tools conventions (for `tools/cost_per_call.py`)
- `python-dotenv` `load_dotenv(PROJECT_ROOT / ".env")`; `PROJECT_ROOT = Path(__file__).resolve().parent.parent`.
- Twilio: `Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])`.
  `call.price` already read once at `tools/twilio_call_diagnostics.py:172`
  (`client.calls(sid).fetch()` → `getattr(d, "price", ...)`). No `Message.price` or
  `usage_records` access exists yet, we add it.
- CLI house style: `argparse`, `def main() -> int`, `if __name__ == "__main__": raise SystemExit(main())`.
- Structured logging via `tools/llm_proxy/logging_config.py` `JsonFormatter`, or `print(json.dumps(...))`.
- Dry-run/`--apply` safety pattern exists but **N/A here**, this tool is read-only by construction.

### 2.6 Rates available today
`PRICING.txt` (updated 2026-05-21) and `OPERATING_COSTS.txt` (2026-06-03) hold all **per-minute**
rates (see §4.1 seed). **Neither holds per-token or per-character rates**, those are needed only
for Phase 2's *measured* lines and must be sourced + flagged (§4.1, §7). No rates are hardcoded in
Python (grep-confirmed). Model is locked to `claude-haiku-4-5-20251001` (`voice_pipeline.py:79-80`).

---

## 3. Architecture decisions (resolved)

| Question (from design) | Resolution |
|---|---|
| Do metrics frames surface tokens/chars, or do we need a fallback? | **They do**, `enable_usage_metrics=True` already on. Read `LLMUsageMetricsData`/`TTSUsageMetricsData` directly. No fallback. |
| Where to accumulate per-call usage? | **In the metrics observer** (it already receives the frames, holds `call_sid`, lives one call, single-threaded on the loop ⇒ no lock). |
| Where to flush the row? | The existing `finally` block (`voice_pipeline.py:551-599`), via `run_in_executor`, right after `save_minute_counter`. |
| New table vs. columns on `transcripts`? | **New `call_usage` table**, matches the one-table-per-concern pattern; keeps the transcript write idempotent and independent. |
| Rate home? | New global `config/cost_rates.json` with `effective_date`. |
| Duration source for modeled lines? | `transcripts.duration_sec` (local, no API). Twilio `call.duration` used only when we're already hitting the API for `call.price`. |

---

## 4. Phase 1, read-only cost tool (no running-code changes, $0)

### 4.1 `config/cost_rates.json` (new)
Single global rate file. **Per-minute rates seed Phase 1; per-token/per-char rates are for Phase 2
and carry a `verified: false` flag until sourced.**

```jsonc
{
  "effective_date": "2026-06-03",
  "currency": "USD",
  "source_notes": "per-minute rates from PRICING.txt (2026-05-21) + OPERATING_COSTS.txt (2026-06-03); token/char rates pending invoice verification",
  "per_minute": {
    "twilio_voice":         { "rate": 0.0085, "verified": true,  "src": "PRICING.txt:10" },
    "twilio_media_streams": { "rate": 0.0040, "verified": true,  "src": "PRICING.txt:11" },
    "deepgram_stt":         { "low": 0.0043, "high": 0.0058, "verified": true, "src": "OPERATING_COSTS.txt:31" },
    "cartesia_tts":         { "low": 0.020, "high": 0.030, "verified": true,  "src": "PRICING.txt:13" },
    "anthropic_haiku":      { "low": 0.005, "high": 0.010, "verified": true,  "src": "PRICING.txt:14" }
  },
  "per_message": {
    "twilio_sms":           { "rate": 0.0079, "a2p_fee": 0.003, "verified": true, "src": "PRICING.txt:15" }
  },
  "per_token": {
    "comment": "Phase 2 measured Anthropic path. Haiku 4.5 list prices, VERIFY against console.anthropic.com / invoice before trusting.",
    "anthropic_haiku_4_5": {
      "input_per_mtok":          1.00,
      "output_per_mtok":         5.00,
      "cache_write_5m_per_mtok": 1.25,
      "cache_read_per_mtok":     0.10,
      "verified": false
    }
  },
  "per_character": {
    "comment": "Phase 2 measured Cartesia path. Derived placeholder = 0.025/min ÷ ~825 chars/min. VERIFY against Cartesia billing/invoice.",
    "cartesia_sonic": { "rate_per_char": 0.0000303, "verified": false }
  }
}
```

### 4.2 `tools/cost_per_call.py` (new)
CLI (argparse), `main() -> int`, dotenv-loaded, read-only:

```
--call-sid SID            cost one call
--since YYYY-MM-DD        cost a date range (with --until); reads transcripts.created_at
--until YYYY-MM-DD
--client-id ID            filter range by client
--db PATH                 default: $TRANSCRIPT_DB_PATH or /data/transcripts.db
--rates PATH              default: config/cost_rates.json
--no-twilio               skip Twilio API; model the voice line + omit SMS (fully offline/$0)
--json                    machine-readable output (else a human table)
```

Data sources & per-line provenance (see §5 for the table):
- **Duration**: `transcripts.duration_sec` for the call_sid(s). (✅ for the quantity.)
- **Twilio Voice**: if Twilio creds present → `client.calls(sid).fetch().price` (✅ measured,
  abs value, account currency). Else → `duration_min × twilio_voice` (〜 modeled).
- **Twilio SMS**: if Twilio creds → `client.messages.list(to=<caller_number>, date_sent_after=...)`
  matching messages in the window **[call_start, call_end + 10 min)**, sum `abs(message.price)`
  (✅ measured price; attribution is best-effort by number + time, so render 〜 on the *attribution*
  and print the matched message SIDs for audit). Else omit with a note.
- **Twilio Media Streams**: `duration_min × twilio_media_streams` (〜 modeled per-call; period
  cross-check via Usage Records is a documented future option, not built now).
- **Deepgram STT**: `duration_min × deepgram_stt` (〜 duration-proxied).
- **Anthropic**: Phase 1 `duration_min × anthropic_haiku` (⚙ modeled). Phase 2 upgrades (§6).
- **Cartesia**: Phase 1 `duration_min × cartesia_tts` (⚙ modeled). Phase 2 upgrades (§6).

Graceful degradation: with `--no-twilio` and no `call_usage` rows, the tool runs fully offline at
$0, every line modeled/proxied from local duration, nothing crashes, markers tell the truth.

Output renders a per-call line-item table with the ✅/〜/⚙ marker per row, a per-call total, and
(for ranges) a grand total + per-provider rollup. Ranges with low/high rates show a low–high band.

### 4.3 Phase-1 tests (`tests/test_cost_per_call.py`, new)
- Rate-file loader: parses seed file; rejects missing `effective_date`.
- Cost math on a synthetic 5-min call with known rates → expected per-line + total.
- Marker correctness: no `call_usage` row ⇒ Anthropic/Cartesia render ⚙; Twilio creds absent ⇒
  Voice renders 〜 and SMS omitted.
- Range aggregation across two fake transcripts.
- Twilio calls are mocked (no network); `--no-twilio` path needs no mock.

---

## 5. Per-line measurement provenance (the legend the tool prints)

| Line | Phase 1 | Phase 2 | Source |
|---|---|---|---|
| Twilio Voice | ✅ measured *(creds)* / 〜 modeled *(--no-twilio)* | same | `call.price` / duration×rate |
| Twilio Media Streams | 〜 modeled | same | duration×rate (Usage Records = future period cross-check) |
| Twilio SMS | ✅ measured msg price, 〜 attribution | same | `message.price` by number+time |
| Deepgram STT | 〜 duration-proxied | 〜 (optional Deepgram usage-API period cross-check, future) | duration×rate |
| Anthropic | ⚙ modeled | **✅ measured** | duration×rate → `call_usage` tokens × per-token |
| Cartesia TTS | ⚙ modeled | **✅ measured** | duration×rate → `call_usage` chars × per-char |

Legend: ✅ measured (billed/metered number) · 〜 metered-but-attributed or duration-proxied with a
period cross-check available · ⚙ modeled (pure duration×per-min estimate).

---

## 6. Phase 2, live measurement (additive, async, hot-path-safe)

### 6.1 `call_usage` table, add to `transcript_store._SCHEMA_SQL`
```sql
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
```
> No `llm_total_tokens` column: Pipecat's `total_tokens` excludes cache tokens (§2.2), so it is a
> misleading half-count and the cost formula (§6.5) never uses it, store only the four component
> counts. A call that ends before any LLM/TTS frame (e.g. early hangup during the greeting) writes a
> row with all counters at 0, **expected and correct**, not an error.

### 6.2 `transcript_store.save_call_usage(...)`, new, mirrors `save_minute_counter`
- `INSERT OR IGNORE` on `call_sid` PK (idempotent; retry-safe).
- Same `_LOCK` + `_get_connection` + best-effort try/except (log & swallow). Never raises.
- `created_at` = ISO-8601 ...Z (same helper style as the existing writes).

### 6.3 Observer extension (`voice_pipeline.py`)

**Import, edit line 43** (extend the existing single-name import, don't add a second line):
```python
# before (line 43):
from pipecat.metrics.metrics import TTFBMetricsData
# after:
from pipecat.metrics.metrics import (
    TTFBMetricsData, LLMUsageMetricsData, TTSUsageMetricsData,
)
```

**Rename, firm decision:** `TTFBLogObserver` → `CallMetricsObserver` (it now records all call
metrics, not just TTFB). Exactly **7 references in 2 files**, update all:
`voice_pipeline.py` docstring `:90`, class def `:121`, instantiation `:471`;
`tests/test_voice_pipeline.py` `:329, :331, :351, :353`. Update the class docstring too.

**`__init__`**, after `self._seen: set[int] = set()` (`:134`), add the accumulators (plain ints
mutated only on the event loop ⇒ **no lock**):
```python
self._llm_prompt = 0
self._llm_completion = 0
self._llm_cache_read = 0
self._llm_cache_creation = 0
self._llm_model: str | None = None
self._tts_chars = 0
self._tts_model: str | None = None
```
No `_llm_total`, Pipecat's `total_tokens` excludes cache tokens (§2.2) and the cost formula
doesn't use it; storing it would be a misleading half-count.

**`on_push_frame`**, inside the existing method, **after the `frame.id` dedup** (`:140-142`) and
before the TTFB log loop, accumulate:
```python
for m in frame.data:
    if isinstance(m, LLMUsageMetricsData):
        u = m.value
        self._llm_prompt         += u.prompt_tokens or 0
        self._llm_completion     += u.completion_tokens or 0
        self._llm_cache_read     += u.cache_read_input_tokens or 0
        self._llm_cache_creation += u.cache_creation_input_tokens or 0
        self._llm_model = self._llm_model or m.model
    elif isinstance(m, TTSUsageMetricsData):
        self._tts_chars += m.value or 0
        self._tts_model = self._tts_model or m.model
for line in _ttfb_log_lines(frame.data, self._call_sid):   # unchanged
    logger.info(line)
```

**`usage_totals()`**, new getter returning EXACTLY the kwargs `save_call_usage` takes (§6.2), so
the finally block can `**`-unpack it directly:
```python
def usage_totals(self) -> dict[str, object]:
    return {
        "llm_model": self._llm_model,
        "llm_prompt_tokens": self._llm_prompt,
        "llm_completion_tokens": self._llm_completion,
        "llm_cache_read_tokens": self._llm_cache_read,
        "llm_cache_creation_tokens": self._llm_cache_creation,
        "tts_model": self._tts_model,
        "tts_characters": self._tts_chars,
    }
```
A pure getter over ints/strs, cannot raise. Accumulation is single-threaded on the event loop ⇒
no lock. `MetricsFrame`s are pushed **downstream-only** in Pipecat 1.2.1, so the `_seen` frame.id
dedup is belt-and-suspenders against re-observation; it does **not** collapse distinct frames from
TTS retries/interruptions (§7 item 6).

### 6.4 Wire-up (`voice_pipeline.py`)
1. **Required hoist.** Today line 471 constructs the observer *inline* inside the `PipelineTask(...)`
   call. Replace that with a named local declared **before** `PipelineTask` (`:468`) so the
   `finally` block can read it:
   ```python
   metrics_observer = CallMetricsObserver(call_sid=call_sid)   # NEW local, before PipelineTask
   task = PipelineTask(
       pipeline,
       params=_build_pipeline_params(TWILIO_SAMPLE_RATE),
       observers=[metrics_observer],                            # was: [TTFBLogObserver(call_sid=call_sid)]
   )
   ```
   This sits above the `try:` at `:541`, the same position the observer is constructed today, so
   it adds **no new** pre-`try` crash surface beyond what already exists.
2. In the `finally` block, right after the `save_minute_counter` executor call (`:578`), flush the
   usage row, wrapped best-effort so a usage-write hiccup can never break call teardown:
   ```python
   try:
       usage = metrics_observer.usage_totals()
       await loop.run_in_executor(
           None,
           functools.partial(
               transcript_store.save_call_usage,
               call_sid=call_sid, client_id=client_id,
               duration_sec=elapsed_sec, **usage,
           ),
       )
   except Exception:
       logger.exception("call_sid=%s call_usage flush failed", call_sid)
   ```
   `save_call_usage` is itself best-effort (errors swallowed inside, §6.2); the outer `try` also
   guards `usage_totals()` and the executor hand-off. Adds **zero** synchronous work to the loop.

### 6.5 Cost-math upgrade in `cost_per_call.py`
When a `call_usage` row exists for a call_sid, replace the modeled Anthropic/Cartesia lines with
measured ones and flip ⚙ → ✅:

```
anthropic_cost =
    prompt_tokens          /1e6 * input_per_mtok
  + completion_tokens      /1e6 * output_per_mtok
  + cache_creation_tokens  /1e6 * cache_write_5m_per_mtok
  + cache_read_tokens      /1e6 * cache_read_per_mtok

cartesia_cost = tts_characters * rate_per_char
```
`total_tokens` is intentionally absent from this formula and the stored row, it excludes cache
tokens (§2.2), so cost is built only from the four component counts. Rate-confidence caveat: the *quantities* (tokens, chars) are ✅ measured, but the per-token /
per-char *rates* are `verified:false` until §7 closes, the tool annotates measured-quantity lines
whose rate is unverified (e.g. `✅* rate est.`) so the distinction stays visible.

### 6.6 Phase-2 tests
- `tests/test_transcript_store.py`: `save_call_usage` round-trip; idempotent re-insert; bad-DB path
  swallows and doesn't raise.
- `tests/test_voice_pipeline.py`: feed the observer a `MetricsFrame` carrying
  `LLMUsageMetricsData` + `TTSUsageMetricsData` (and a `TTFBMetricsData`) → `usage_totals()` sums
  correctly; duplicate `frame.id` doesn't double-count; TTFB logging still works (no regression to
  the existing `:302-319` test).
- `cost_per_call.py`: with a `call_usage` row, Anthropic/Cartesia render ✅ and use the token/char
  math; without one, they fall back to ⚙ modeled.

---

## 7. Open items to verify during implementation
1. **Anthropic Haiku 4.5 token prices**, seed values in `cost_rates.json.per_token` are list-price
   estimates (`verified:false`). Confirm against console.anthropic.com pricing or a real invoice;
   flip `verified:true` once sourced. (Blocks only Phase 2's Anthropic ✅ line, not Phase 1.)
2. **Cartesia per-character rate**, placeholder is *derived* (0.025/min ÷ ~825 chars/min). Confirm
   against Cartesia billing/an invoice. Until then the measured-char line carries `rate est.`.
3. **`prompt_tokens` excludes cached tokens**, asserted from Anthropic `usage` semantics +
   `pipecat/services/anthropic/llm.py:442-476`. Confirm with one real `call_usage` row vs. the
   Anthropic dashboard for the same call (no double-counting of cache tokens in the cost sum).
4. **Twilio `price` timing/sign**, `call.price`/`message.price` are populated *after* the
   resource finalizes and are negative strings in account currency; the tool must `abs()` and
   handle `None`/`""` for not-yet-priced very recent calls (render 〜 "pending").
5. **SMS attribution window**, fixed at **[call_start, call_end + 10 min)** to the caller's
   number (§4.2); surface the matched message SIDs so a human can sanity-check.
6. **TTS-retry overcount**, `tts_characters` sums `len(text)` per `TTSUsageMetricsData`. The
   `frame.id` dedup stops re-observation of the *same* frame, but an interrupted/retried utterance
   yields *distinct* frames (`cartesia/tts.py:730, 979`) that both count, a small overcount when
   barge-in is frequent. Accept as a known bias for v1; surface a per-call interruption count so the
   bias is visible, and dedup by sentence-hash later only if retry rates prove high.
7. **Interpreter pin**, verify any pipecat/field assumption with `.venv/bin/python`, never a bare
   system `python3` (which may carry an older pipecat). See §2.2.

---

## 8. Sequencing
1. **Phase 1a**, `config/cost_rates.json` + `tools/cost_per_call.py` (offline/`--no-twilio` path)
   + tests. Fully $0, no running-code change. Ship & use immediately on past calls.
2. **Phase 1b**, add the Twilio `call.price` + SMS `message.price` API path (still read-only).
3. **Phase 2a**, `call_usage` table + `save_call_usage` + tests (storage only; no call-path change yet).
4. **Phase 2b**, observer accumulation + `finally`-block flush + tests (the only hot-path edit;
   additive, async, best-effort).
5. **Phase 2c**, `cost_per_call.py` ⚙→✅ upgrade when `call_usage` present; close §7 rate items.

Each step is independently shippable and reversible. Phases 1a/1b touch no running code; the only
live-path change is 2b, which mirrors an existing best-effort async write and adds no sync work to
the event loop.
