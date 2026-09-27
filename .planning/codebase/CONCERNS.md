# Codebase Concerns

**Analysis Date:** 2026-05-25

---

## Critical Issues

### 1. Synchronous Google Sheets I/O blocks the async event loop (HIGH)

`tools/llm_proxy/function_tools.py:288`, `sheets_writer.append_row(row)` is a synchronous
call to the Google Sheets REST API made directly inside the `async def log_request` handler,
without `asyncio.to_thread`. The Google API client (`googleapiclient`) uses the `requests`
library, which is blocking. This stalls the entire uvicorn event loop for the duration of
the HTTP round-trip to Google (typically 200–600 ms). Every concurrent call and WebSocket
frame is frozen while one caller's intake row is being written.

The same pattern appears in `tools/llm_proxy/voicemail.py:236-237` (`ensure_tab` and
`append_to_tab` called from `async def handle_recording_callback`) and line 306
(`update_voicemail_transcript` called from `async def handle_transcription_callback`).

Fix: wrap every `sheets_writer.*` call in `asyncio.to_thread(lambda: sheets_writer.append_row(row))`.

---

### 2. `voicemail_pending` and `voicemail_transcripts` dicts are never garbage-collected (MEDIUM)

`tools/llm_proxy/main.py:362-363` creates two in-memory dicts on `app.state` that grow
without bound. `voicemail.py:256` does call `pending.pop(recording_sid, None)` inside
`_await_and_send` after the event fires or times out. However, if the background task
is GC'd before completion (edge case, the strong-ref set at line 275 usually prevents
this), or if Twilio never fires the recording callback at all, entries accumulate. The
`transcripts` dict (`state.voicemail_transcripts`) has no cleanup path if the recording
callback fires but the background task crashes before calling `transcripts.pop`. At low
voicemail volume this is harmless; at sustained volume it is a slow memory leak.

Fix: add a TTL sweep (e.g., on a periodic asyncio task or on each new voicemail callback)
that removes entries older than `wait_timeout_seconds * 2`.

---

### 3. `RECORDING_RETENTION_DAYS = 30` contradicts the documented 1-day audio retention policy (MEDIUM–HIGH)

`tools/llm_proxy/voicemail.py:195` sets `RECORDING_RETENTION_DAYS = 30`. This value is used
at line 225 to compute `expires_at`, the expiry timestamp written into the Google Sheet and
used to sign the playback token. However, `tools/voicemail_retention.py:120` defaults
`--audio-days` to `1`, meaning the actual Twilio recording is deleted after ~24 hours.

The signed playback URL will be valid for 30 days but the underlying audio will be gone
after 1 day. The result: the owner receives an SMS with a working-looking link that returns
410 Gone the next day. The Sheet's `playback_token_expires_at` column is also misleading.

Fix: align `RECORDING_RETENTION_DAYS` with the actual `--audio-days` default (1), or
document that the token lifetime intentionally differs from audio lifetime. At minimum,
rename the constant to `PLAYBACK_URL_EXPIRY_DAYS` so the distinction is clear.

---

### 4. `datetime.utcnow()` used in demo-token logic, naive datetimes in Python 3.12+ (LOW–MEDIUM)

`tools/llm_proxy/main.py:125, 273, 280` use `datetime.utcnow()`, which is deprecated in
Python 3.12 and returns a naive datetime (no timezone info). The comparison at line 280
(`datetime.utcnow() > expiry`) works only because both sides are naive, but this is
fragile, any refactor that makes one side timezone-aware will silently break token
validation with a `TypeError` at runtime.

Fix: replace all three instances with `datetime.now(timezone.utc)` to match the pattern
used everywhere else in the codebase (e.g., `transcript_store.py:99`, `voicemail.py:222`).

---

### 5. `_CONNECTIONS` module-level dict in `transcript_store.py` is a process-wide singleton (LOW)

`tools/llm_proxy/transcript_store.py:50-51`, `_CONNECTIONS` and `_LOCK` are module-level
globals that cache SQLite connections keyed by `db_path`. Because the module is imported
once and never reloaded, the connection is shared across all request handlers. This is
intentional (documented), but the connection is created with `check_same_thread=False`,
meaning any corruption from concurrent writes would produce opaque errors rather than a
clear exception. WAL mode mitigates concurrent reads, but concurrent writers still
serialize through `_LOCK`. If a writer thread panics mid-transaction, the connection
state is left indeterminate for all subsequent callers.

Fix (medium-term): wrap each `save()` call with a fresh `connect()` that is opened and
closed atomically, or use a connection pool. Current `timeout=5.0` at least prevents
indefinite blocking.

---

## Architecture Concerns

### 6. Single Twilio account for all clients, hard blocks true multi-tenancy (HIGH)

`tools/llm_proxy/main.py:180, 233`, a single `TWILIO_AUTH_TOKEN` env var is used for
all clients (`ClientContext.auth_token = global_auth_token`). The multi-client config
(`scan_client_configs`) supports N tenants but they all share one Twilio account. The
`voicemail.py` signature validator (`_validate_twilio_signature`) also reads the global
`TWILIO_AUTH_TOKEN` (line 69) rather than a per-client token.

Consequence: adding a second business that has its own Twilio sub-account is impossible
without refactoring. The `ClientConfig` has no `twilio_auth_token` field even though the
`ClientContext` tries to expose one (`auth_token` field).

Fix: add `twilio_auth_token` and `twilio_account_sid` to `ClientConfig` (JSON-driven),
remove the global `TWILIO_AUTH_TOKEN` as the sole source, and pass the per-client token
into `_validate_twilio_signature` for voicemail callbacks.

---

### 7. Voicemail callbacks are not per-client routed, silent wrong-client attribution (MEDIUM)

`tools/llm_proxy/main.py:368-373`, the voicemail recording/transcription callbacks lack
a `To` header from Twilio, so the app hardwires the first live client's `sheets_writer`
and `sms_notifier` as `app.state.voicemail_sheets_writer` / `app.state.voicemail_sms_notifier`.
Any second live client's voicemails will be silently appended to the first live client's
Sheet and the first client's owner will receive the SMS.

This is acknowledged in a comment ("v1 supports one voicemail-handling client") but the
hazard of wrong-client data attribution is not called out. At two live clients, this is
a data-integrity bug.

Fix (near-term): encode the client's `env_prefix` or `client_id` as a `<Parameter>` in
the voicemail TwiML's `<Record>` callback URL (e.g., as a query param), then read it in
the callback handler to dispatch to the correct client context.

---

### 8. GDPR and PIPA compliance text is hardcoded in `voice_pipeline.py`, not in prompt files (MEDIUM)

`tools/llm_proxy/voice_pipeline.py:81-198`, 118 lines of legal/compliance text
(`GDPR_PROMPT_PREFIX` and `PIPA_PROMPT_PREFIX`) are hardcoded Python string constants.
This violates the codebase's own prompt-loading pattern: business text is meant to live
in `tools/prompts/`, be versioned by content hash, and be hot-reloadable via restart.

The compliance prompts bypass the `LoadedPrompt` version hash entirely, there is no way
to audit which GDPR/PIPA text a specific call received without reading the source code
at that git commit. Legal text changes require a code deploy, not a prompt file update.

Fix: move both prefixes to `tools/prompts/gdpr_prefix.md` and `tools/prompts/pipa_prefix.md`,
load them in `prompt_loader.load_prompt()` when the relevant config flag is set, and fold
them into the `LoadedPrompt.version` hash so they appear in logs.

---

### 9. `human_handoff` reuses `send_owner_emergency_alert`, semantic mismatch (LOW)

`tools/llm_proxy/function_tools.py:363-368`, the `human_handoff` handler calls
`sms_notifier.send_owner_emergency_alert(pest="HUMAN HANDOFF REQUESTED", ...)`. This
reuses a method that formats an "EMERGENCY intake" message template, cramming a
human-handoff event into a pest-alert format. The owner SMS reads:
`"EMERGENCY intake, Clear Slope\nPest: HUMAN HANDOFF REQUESTED"`, which is confusing.

Fix: add a dedicated `send_human_handoff_alert()` method to `SmsNotifier` with an
appropriate template, and use it in the `human_handoff` handler.

---

### 10. Demo path hard-codes `client_id == "clearslope"` as the demo client (LOW)

`tools/llm_proxy/main.py:258`, the demo `/text-chat` endpoint falls back to the first
registered client if "clearslope" is not found, but the primary lookup is by string literal.
Similarly, line 152-153 hardcodes `clearslopedigital.com` in the CORS whitelist. Both
are single-tenant assumptions baked into what is otherwise a multi-tenant app.

Fix: expose a `demo_client_id` field in a top-level server config (or as an env var),
and move the CORS origin list to environment configuration so it can change per deployment.

---

## Optimization Opportunities

### 11. Google Sheets append on the live call path adds 200–600 ms to call teardown (MEDIUM)

`tools/llm_proxy/function_tools.py:288`, as noted in Issue 1, `sheets_writer.append_row`
is synchronous. Even if wrapped in `asyncio.to_thread`, the call happens inline with the
LLM function-tool response. The caller hears silence while the Google API round-trip
completes before the model's "you're all set" line is spoken.

Fix: defer the Sheets write to a fire-and-forget `asyncio.create_task`. The model's
`result_callback` with `{"status": "saved"}` can be returned immediately; the actual
write happens in the background. (Keep the error log on failure; the sheet row is
non-critical for the caller's experience.)

---

### 12. Playback proxy buffers the entire MP3 in memory before responding (LOW)

`tools/llm_proxy/voicemail.py:367-383`, `httpx.AsyncClient.get()` is called without
streaming, so `resp.content` loads the full Twilio MP3 (up to 2 minutes × ~128 kbps ≈
1.9 MB) into the server's heap before the `Response` is returned to the browser. On
`shared-cpu-1x` (256 MB RAM), several concurrent playback requests could saturate memory.

Fix: use `httpx.AsyncClient.stream()` and return a `StreamingResponse` so bytes are
proxied chunk-by-chunk without buffering.

---

### 13. `PIIRedactor._get_redactor()` global init happens on first end-of-call, adding latency (LOW)

`tools/llm_proxy/voice_pipeline.py:229-248`, `_get_redactor()` lazy-inits the Presidio
`AnalyzerEngine` (which loads a spaCy `en_core_web_lg` model) on the first call's cleanup
path. The comment acknowledges the load is expensive. While the `_REDACTOR_INITIALIZED`
flag prevents repeated loads, the first call after process start pays ~2–5 seconds of
blocking model-load time inside the `finally` block of `run_voice_session`.

Since `main.py` already has a `_warm_pipecat_models()` call at startup, the same pattern
should apply to the redactor: call `_get_redactor()` during app startup (eagerly, blocking,
before Fly health probes succeed) so first-call cleanup is not delayed.

---

### 14. `update_voicemail_transcript` does a full sheet read on every late transcription (LOW)

`tools/llm_proxy/sheets_writer.py:311-353`, when Twilio's transcription callback arrives
after the 60-second timeout, `update_voicemail_transcript` fetches the entire Voicemails
tab (`A:H`) to find the row by `recording_sid`. At 10+ voicemails/day this grows from
O(1) to O(n rows) on every late transcript. The Google Sheets API also counts this as a
read unit against the quota.

Fix (acceptable for current scale): cache the `{recording_sid: row_index}` mapping in
memory when rows are appended, and use it for direct-cell updates. Or use `VLOOKUP`-style
named ranges if the Sheet grows large.

---

### 15. `SileroVADAnalyzer` is instantiated fresh for every call despite warm model (MEDIUM)

`tools/llm_proxy/voice_pipeline.py:413`, `SileroVADAnalyzer()` is called inside
`run_voice_session`, so each call loads a new analyzer instance. `main.py` warms the
model in `_warm_pipecat_models` (lines 77-86) and stores it on `app.state.warmed_vad`,
but `run_voice_session` never uses the warmed instance. The comment in `_warm_pipecat_models`
acknowledges that per-call construction is intentional because "sharing them across
concurrent calls would mix VAD audio-history buffers." This is correct, the concern is
construction cost, not reuse. At ~100 ms per construction (post-warm), this is acceptable
overhead per call but worth measuring if call setup latency climbs.

No immediate fix needed; document this explicitly as a known cost so future developers
don't attempt to share the instance across calls.

---

## Scalability Bottlenecks

### 16. `pending_calls` dict is a race-condition point under concurrent calls (MEDIUM)

`tools/llm_proxy/voice_routes.py:135, 209`, `POST /twilio/voice` writes
`state.pending_calls[call_sid] = to_number` and `WS /twilio/stream` reads/pops it.
Because these are two separate HTTP/WS requests (potentially handled by different
asyncio tasks), there is a narrow window where the WS arrives before the POST has
written to the dict. The current code handles this (line 210-215 logs and drops the WS
connection). However, if Twilio's timing ever reverses (WS precedes POST), the call
silently drops. There is no retry or wait mechanism.

More critically: at higher concurrency, if a POST completes but Twilio never opens the
WS (e.g., network drop), the entry in `pending_calls` is never cleaned up. Over time
on a busy deployment this dict grows. An attacker who can spoof a `POST /twilio/voice`
(bypassing signature validation via a misconfigured `ALLOW_UNSIGNED_TWILIO`) can inflate
this dict without bound.

Fix: add a TTL to entries (e.g., prune entries older than 30 seconds on each incoming WS),
or use an `asyncio.Event`-based bridge with a timeout.

---

### 17. Minute-ceiling counter resets to zero on every process restart (MEDIUM)

`tools/llm_proxy/main.py:264`, `app.state.minute_counters` is initialized to `{client_id: 0.0}`
at app startup. On Fly, this happens every deploy, every machine restart, and every
`fly machine restart`. The ceiling check (line 113-127 in `voice_routes.py`) only sees
minutes accumulated in the current process lifetime. A client who has used 95% of their
monthly ceiling will have full access again after any restart.

Fix: persist `minute_counters` in the SQLite store (a single-row `counters` table per
client per billing period) and load it at startup. Or use a Fly KV / Redis for simplicity.

---

### 18. SQLite single-writer constraint blocks multi-machine deployments (MEDIUM)

`tools/llm_proxy/transcript_store.py:1-17`, the docstring explicitly acknowledges this:
"when we scale to multiple machines, this file becomes the bottleneck." The Fly volume
(`/data/transcripts.db`) is single-machine. If the app scales to two Fly machines (or
if Fly auto-restarts on a different machine), the two processes would each have their own
SQLite DB, producing silently split transcript histories.

This is a known accepted risk at current scale, but it means any horizontal scaling
attempt will produce data divergence without a migration plan in place.

Fix (long-term): migrate to Fly Postgres as documented. Fix (short-term): add a startup
assertion that verifies the volume is mounted and logs the machine ID, so split-brain
scenarios are immediately visible in logs.

---

### 19. `TOOLS_SCHEMA` is module-level and includes all tools for all compliance modes (LOW)

`tools/llm_proxy/function_tools.py:195-203`, `TOOLS_SCHEMA` is a single global containing
all 5 tools including `set_recording_preference`, `human_handoff`, and `transfer_to_voicemail`.
These tools are always sent to Claude even for clients in bare mode (non-GDPR, non-PIPA)
where `set_recording_preference` has no meaning and `human_handoff` is untested.

More tools in the schema = more tokens sent per request, more chance of the model
invoking an unexpected tool, and a larger prompt-cache key (which reduces cache hit rate).

Fix: build the `ToolsSchema` dynamically in `run_voice_session` based on the client's
`gdpr_mode`, `pipa_mode`, and configured capabilities, registering only the tools that
are active for that session.

---

### 20. `voicemail_retention.py` has hardcoded `--prefix ALLGREEN` default (MEDIUM)

`tools/voicemail_retention.py:122`, the CLI default for `--prefix` is `"ALLGREEN"`,
a specific client's env prefix. If this script is ever run without `--prefix` on the
Clearslope deployment, it will silently use the wrong client's Sheet credentials and
either write to the wrong Sheet or fail silently. This is an ops footgun.

Fix: remove the default (require `--prefix` explicitly) or source it from an env var
like `DEFAULT_CLIENT_PREFIX`. Add a validation warning if the provided prefix matches
no configured client.

---

## Modularization Issues

### 21. `voice_pipeline.py` is a 564-line file doing too much (MEDIUM)

`tools/llm_proxy/voice_pipeline.py` combines:
- Pipecat pipeline wiring (`run_voice_session`, lines 355–565)
- PII redaction orchestration (`_persist_redacted_transcript`, `_get_redactor`, lines 225–353)
- Transcript text extraction (`_extract_transcript_text`, lines 251–282)
- Compliance prompt assembly (`_compose_system_prompt`, `GDPR_PROMPT_PREFIX`, `PIPA_PROMPT_PREFIX`, lines 81–208)
- PIPA greeting rendering (`_render_pipa_greeting`, lines 211–222)
- Process-wide redactor singleton management (lines 225–248)

The transcript persistence and redaction logic should live in a dedicated
`transcript_pipeline.py` module that `voice_pipeline.py` calls. The compliance prompt
prefixes should live in prompt files (see Issue 8). This would bring `voice_pipeline.py`
below 200 lines and make the redaction path independently testable.

---

### 22. `main.py` is a 429-line god module for app wiring (LOW–MEDIUM)

`tools/llm_proxy/main.py` contains:
- App factory (`_build_app`)
- Sentry initialization (`_init_sentry`)
- CORS and rate limiter setup
- All client context bootstrap logic (lines 188–234)
- Demo visitor CSV logging (`_log_visitor`, lines 117–129)
- In-memory demo token store and validation (lines 268–281)
- HTTP middleware
- Healthz endpoint
- Demo auth endpoint
- `/text-chat` endpoint
- Voicemail state initialization
- Pipecat model warm-up

The demo auth/token logic and visitor logging belong in a `demo_auth.py` module.
The client bootstrap loop belongs in a `startup.py` or `client_registry.py`. This
separation would make each piece unit-testable without constructing the full app.

---

### 23. `SheetsWriter` handles both booking rows and voicemail rows, two schemas in one class (LOW)

`tools/llm_proxy/sheets_writer.py` manages intake bookings (Sheet1), voicemail rows
(Voicemails tab), and their deletion and update operations. The `read_all_rows` method
is used for the weekly digest. The class is 374 lines and the `delete_voicemail_row`
method lazily fetches the sheet's internal `sheetId` on first call and caches it as
`_voicemail_sheet_id`, a stateful side-effect that makes the class harder to test
and mock.

Fix: split into `BookingSheet` and `VoicemailSheet` with a shared `_GoogleSheetsBase`.
This makes each independently replaceable when the data store migrates.

---

### 24. `function_tools.py` couples LLM tool handling to Sheets, SMS, and Twilio (LOW)

`tools/llm_proxy/function_tools.py` imports from `sheets_writer`, `sms_notifier`, and
`voicemail`. The `make_handlers()` factory takes concrete `SheetsWriter | None` and
`SmsNotifier | None` instances. This means unit tests for tool behavior must construct
or mock all three external services. The `log_request` handler makes a synchronous
Sheets call (Issue 1), fires SMS, and updates recording state, three separate concerns
in one function.

Fix: define `SheetsSink` and `SmsSink` protocols (structural typing), so tests can pass
lightweight fakes. Extract the "should we send SMS?" decision logic into a pure function
that tests can exercise without side effects.

---

## Technical Debt Inventory

### D-01 | `_public_base_url` is duplicated verbatim in two modules, HIGH impact

`tools/llm_proxy/voice_routes.py:46-59` and `tools/llm_proxy/voicemail.py:44-59`, identical 14-line functions with the same logic and different return schemes (`wss://`
vs `https://`). A bug fix in one requires remembering to fix the other. Extract to a
shared `url_utils.py` with `public_ws_base_url()` and `public_https_base_url()` helpers.

---

### D-02 | Magic string `"clearslope"` scattered across codebase, MEDIUM impact

`tools/llm_proxy/main.py:258` uses the literal `"clearslope"` as the demo client_id.
`tools/llm_proxy/sheets_writer.py:2` (docstring). `tools/llm_proxy/sms_notifier.py:63-64`
default `business_name` parameters are Clear Slope-specific. These are fine for the
current single paying client but will cause confusion when a second client is onboarded.

---

### D-03 | `CARTESIA_VOICE_ID` is a single global env var, blocks per-client TTS persona, MEDIUM impact

`tools/llm_proxy/voice_pipeline.py:385` reads `CARTESIA_VOICE_ID` from the environment
without any per-client override. If two clients are onboarded with different AI
names (Sarah vs Emma), they must share the same voice, breaking the persona. Add
`cartesia_voice_id` to `ClientConfig` (with env-var interpolation) so each client can
configure their own TTS voice.

---

### D-04 | `human_handoff` has no real transfer, GDPR Article 22 gap, HIGH impact

`tools/llm_proxy/function_tools.py:344` (docstring), "A live Twilio `<Dial>` redirect
is on the roadmap." Currently `human_handoff` sends an owner SMS and ends the call.
The caller is told "someone will call you back." Under GDPR Article 22, the right to
human intervention must be honored; an SMS-then-callback-later pattern may not satisfy
regulators for synchronous automated-decision processes. This is a legal/compliance gap
that has been acknowledged but not scheduled.

---

### D-05 | `voicemail_twiml` endpoint has no Twilio signature check, MEDIUM impact

`tools/llm_proxy/voicemail.py:87-108`, the `POST /twilio/voicemail.xml` endpoint does
NOT call `_validate_twilio_signature`. Any HTTP client that can discover the URL can
trigger a TwiML response (though it cannot redirect a real call without Twilio's
involvement). The recording and transcription callbacks DO validate, so this is a partial
gap. Fix: add `_validate_twilio_signature` to `voicemail_twiml` and pass a form dict
(which for this endpoint would be the Twilio call parameters).

---

### D-06 | Demo token store (`_demo_tokens` dict) has no persistence or max-size cap, LOW impact

`tools/llm_proxy/main.py:268`, `_demo_tokens` is a local dict inside `_build_app` that
grows by one entry per login and is only pruned on failed `_validate_token` lookups.
If the demo gets significant traffic (or is hit by a token-farming bot), this dict grows
unboundedly. At 8-hour TTL and typical usage this is negligible, but it has no size cap.

---

## Quick Wins

1. **Fix blocking Sheets calls in async handlers** (`function_tools.py:288`, `voicemail.py:236-237, 306`),    wrap each call in `asyncio.to_thread`. ~30 minutes. Directly improves call responsiveness and
   prevents event-loop stall on every log_request invocation.

2. **Fix `datetime.utcnow()` → `datetime.now(timezone.utc)`** (`main.py:125, 273, 280`),    3-line change. Removes a Python 3.12 deprecation warning and aligns with the rest of
   the codebase.

3. **Align `RECORDING_RETENTION_DAYS` with actual audio retention** (`voicemail.py:195`),    change `30` to `1` (or rename the constant to reflect URL expiry vs audio lifetime) and
   add a one-line comment explaining the relationship to `--audio-days`.

4. **Extract `_public_base_url` duplication** (`voice_routes.py:46-59`, `voicemail.py:44-59`),    move to a `tools/llm_proxy/url_utils.py`. ~20 minutes. Prevents a future silent
   divergence between the two implementations.

5. **Eager-init the PIIRedactor at startup** (`voice_pipeline.py:229-248`),    call `_get_redactor()` from `_build_app()` before Fly health probes succeed,
   alongside `_warm_pipecat_models()`. ~5-minute change. Eliminates the 2–5 second
   blocking redaction-model load on the first call's cleanup path.

6. **Remove `--prefix ALLGREEN` default from `voicemail_retention.py`** (line 122),    1-line change to make the argument required. Prevents wrong-client Sheet writes on
   the Clearslope deployment if someone runs the script without specifying the prefix.

7. **Add TTL cleanup to `pending_calls` dict** (`voice_routes.py`),    on each incoming WS connection, pop entries older than 30 seconds from
   `state.pending_calls` before the main lookup. Prevents unbounded dict growth
   from calls that open a TwiML request but never open the WS.

---

*Concerns audit: 2026-05-25*
