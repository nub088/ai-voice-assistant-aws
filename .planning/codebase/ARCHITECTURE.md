<!-- refreshed: 2026-05-25 -->
# Architecture

**Analysis Date:** 2026-05-25

## System Overview

An AI phone receptionist that intercepts inbound Twilio calls, streams audio through a Pipecat pipeline (Deepgram STT → Claude Haiku → Cartesia TTS), collects a structured pest-control service intake, and logs bookings to Google Sheets with post-call SMS notifications. The system runs as a single FastAPI process on Fly.io and serves multiple clients from one machine, routing each call to the correct tenant by the inbound Twilio number.

## Architectural Pattern

**Single-process multi-tenant FastAPI service with a dual-surface design:**

```text
┌────────────────────────────────────────────────────────────────────────┐
│  FastAPI App  (tools/llm_proxy/main.py)                                │
│                                                                         │
│  ┌────────────────────────────────┐  ┌────────────────────────────┐   │
│  │  Voice Surface                 │  │  Demo Surface              │   │
│  │  POST /twilio/voice (TwiML)    │  │  POST /auth/demo (login)   │   │
│  │  WS   /twilio/stream (audio)   │  │  POST /text-chat (chat)    │   │
│  │  POST /twilio/voicemail.xml    │  │                            │   │
│  │  POST /twilio/voicemail/*      │  │  Same system prompt;       │   │
│  │  GET  /vm/{recording_sid}      │  │  stateless, text-only      │   │
│  └──────────────┬─────────────────┘  └────────────────────────────┘   │
│                 │                                                        │
│  ┌──────────────▼──────────────────────────────────────────────────┐  │
│  │  app.state.clients  dict[twilio_number, ClientContext]           │  │
│  │  ClientContext = { config, loaded_prompt, sheets_writer,         │  │
│  │                    sms_notifier, auth_token }                    │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
         │ per-call
         ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Pipecat Pipeline  (tools/llm_proxy/voice_pipeline.py)                 │
│                                                                         │
│  Twilio WS → TwilioFrameSerializer → SileroVAD                         │
│           → DeepgramSTTService                                          │
│           → LLMContextAggregator (user)                                │
│           → AnthropicLLMService (Haiku 4.5, prompt-cached)             │
│           → CartesiaTTSService (8 kHz)                                  │
│           → TwilioFrameSerializer → Twilio WS                          │
│           → LLMContextAggregator (assistant)                            │
└────────────────────────────────────────────────────────────────────────┘
         │ tool calls (LLM-invoked)
         ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Function Tools  (tools/llm_proxy/function_tools.py)                   │
│                                                                         │
│  log_request → SheetsWriter.append_row + SmsNotifier.send_*            │
│  end_call    → EndTaskFrame → pipeline drain                            │
│  human_handoff → SmsNotifier.send_owner_emergency_alert + end_call     │
│  transfer_to_voicemail → Twilio REST calls.update + end_call           │
│  set_recording_preference → recording_state dict (in-memory per call)  │
└────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Output Layer                                                            │
│                                                                          │
│ Google Sheets (SheetsWriter), booking intake + Voicemails tab │
│ Twilio SMS (SmsNotifier), customer confirmation + owner alerts │
│ SQLite (TranscriptStore), redacted transcripts at /data/ │
└─────────────────────────────────────────────────────────────────────────┘
```

## Core Data Flow

### Live Voice Call (primary path)

1. **Twilio dials the app**, POST `https://<app>/twilio/voice`
   - `voice_routes.py: twilio_voice()` extracts `To` number, looks up `ClientContext` from `app.state.clients`
   - Validates Twilio HMAC-SHA1 signature
   - Checks per-client minute ceiling (`app.state.minute_counters`)
   - Stores `call_sid → to_number` in `app.state.pending_calls`
   - Returns TwiML `<Connect><Stream url="wss://<app>/twilio/stream"/>` 

2. **Twilio opens Media Stream WebSocket**, WS `wss://<app>/twilio/stream`
   - `voice_routes.py: twilio_stream()` reads `connected` + `start` control frames
   - Recovers `ClientContext` via `pending_calls[call_sid]` bridge
   - Calls `voice_pipeline.run_voice_session()`

3. **Pipecat pipeline runs for the call duration**
   - `voice_pipeline.py: run_voice_session()` wires the pipeline (see diagram above)
   - Composes system prompt: `[GDPR/PIPA prefix] + time_context(config) + loaded_prompt.text`
   - Registers per-call tool handlers via `function_tools.make_handlers()`
   - On client connect: queues greeting (PIPA: deterministic TTS; others: LLM-generated via `LLMRunFrame`)
   - Audio frames flow: μ-law 8kHz from Twilio → Deepgram STT → context aggregator → Haiku → Cartesia TTS → back to Twilio

4. **Booking logged (model invokes `log_request` tool)**
   - `function_tools.py: log_request()` formats a row via `sheets_writer.format_row()` and calls `sheets_writer.append_row()`
   - If `urgency == "emergency"` and `sms_notifier` configured: fires `send_owner_emergency_alert()`
   - If `caller_number` known: fires `send_customer_confirmation()`

5. **Call ends (model invokes `end_call` or caller hangs up)**
   - Pipeline `finally` block: computes wall-clock minutes, updates `minute_counters[client_id]`
   - `_persist_redacted_transcript()`: Presidio + optional Claude PII redaction → JSON log line + `transcript_store.save()` (SQLite on `/data/`)

### Voicemail Fallback Path

1. Model invokes `transfer_to_voicemail` → `voicemail.py: make_transfer_to_voicemail_handler()` calls Twilio REST `calls.update(url=/twilio/voicemail.xml)`
2. Twilio redirects call to `/twilio/voicemail.xml` → returns `<Record transcribe=true>` TwiML
3. Twilio POSTs to `/twilio/voicemail/recording` → `handle_recording_callback()` appends Voicemails sheet row, spawns background task waiting for transcript
4. Twilio POSTs to `/twilio/voicemail/transcription` → `handle_transcription_callback()` unblocks the background task
5. Background task: sends owner SMS with signed playback URL (`/vm/{recording_sid}?exp=...&sig=...`)
6. Owner clicks link → `GET /vm/{recording_sid}` → `voicemail_urls.verify_playback_token()` → HTTP proxy streams MP3 from Twilio

### Demo Text-Chat Path

- `POST /auth/demo` → issues in-memory 8-hour token
- `POST /text-chat` → `text_chat_handler.handle_text_chat()`, stateless, caller sends full history each turn, same Haiku model + system prompt, no tool registration, strips `<log_request>` / `<end_call>` XML tags from response

## Component Responsibilities

| Component | File | Owns | Delegates to |
|-----------|------|------|--------------|
| **App factory** | `tools/llm_proxy/main.py` | Startup, middleware, routes, `app.state`, `/healthz`, `/auth/demo`, `/text-chat` | `voice_router`, `voicemail_router`, `client_config`, `prompt_loader`, `sheets_writer`, `sms_notifier` |
| **Voice routes** | `tools/llm_proxy/voice_routes.py` | `/twilio/voice` (TwiML) and `/twilio/stream` (WS accept + handoff) | `voice_pipeline.run_voice_session()` |
| **Voice pipeline** | `tools/llm_proxy/voice_pipeline.py` | Pipecat pipeline construction, greeting kickoff, minute accounting, end-of-call transcript persistence | `function_tools.make_handlers()`, `transcript_store.save()`, `redact_pii` |
| **Function tools** | `tools/llm_proxy/function_tools.py` | Per-call tool handler closures (`log_request`, `end_call`, `human_handoff`, `set_recording_preference`, `transfer_to_voicemail`), recording-consent state machine | `SheetsWriter`, `SmsNotifier`, `voicemail.make_transfer_to_voicemail_handler()` |
| **Client config** | `tools/llm_proxy/client_config.py` | Loading and validating `config/clients/*.json`; `ClientConfig` and `ClientContext` dataclasses; `scan_client_configs()` | Nothing (pure data) |
| **Prompt loader** | `tools/llm_proxy/prompt_loader.py` | Loading + interpolating system prompt + pest taxonomy; computing `time_context()` | `client_config.interpolate()` |
| **Sheets writer** | `tools/llm_proxy/sheets_writer.py` | Google Sheets append, header management, voicemail tab, weekly digest tab, per-row transcript-retain flag | Google Sheets API v4 |
| **SMS notifier** | `tools/llm_proxy/sms_notifier.py` | Customer confirmation SMS, owner emergency alert, voicemail alert; dispatches to thread via `asyncio.to_thread` | Twilio REST messages.create |
| **Voicemail** | `tools/llm_proxy/voicemail.py` | Voicemail TwiML, recording/transcription callbacks, playback proxy, transfer-to-voicemail handler factory | `voicemail_urls`, `SheetsWriter`, `SmsNotifier`, Twilio REST `calls.update`, httpx |
| **Voicemail URLs** | `tools/llm_proxy/voicemail_urls.py` | HMAC-SHA256 signed playback token generation and verification | Nothing (pure crypto) |
| **Transcript store** | `tools/llm_proxy/transcript_store.py` | SQLite WAL writes at `/data/transcripts.db`; best-effort, never raises into call path | sqlite3 |
| **Text chat handler** | `tools/llm_proxy/text_chat_handler.py` | Stateless `/text-chat` request: Anthropic call, tag stripping, `ended` flag detection | `anthropic.AsyncAnthropic` |
| **Digest** | `tools/llm_proxy/digest.py` | Parse Sheet rows → `CallRow` → `Digest`; render text + sheet row | `SheetsWriter.read_all_rows()` |
| **Digest CLI** | `tools/llm_proxy/digest_cli.py` | CLI entrypoint for weekly digest; writes WeeklyDigest tab | `digest.py`, `SheetsWriter` |
| **PII redactor** | `tools/redact_pii.py` | Presidio NER + HMAC-hash filenames; audit pass for residual PII | presidio_analyzer, spaCy |
| **Retention enforcer** | `tools/retention_enforcer.py` | Daily scheduled cleanup of old transcripts/recordings | Runs as Fly `retention` process |

## Key Design Decisions

**WebSocket (Media Streams) not REST for audio:**
Twilio's Programmable Voice + Media Streams delivers bidirectional μ-law 8 kHz audio over a persistent WebSocket. REST polling would introduce unacceptable latency for real-time voice. The WS protocol also lets Twilio push audio frames without the app polling, and lets the app send audio back incrementally as TTS produces it.

**Pipecat as the voice pipeline framework:**
Pipecat provides a frame-based pipeline abstraction that handles audio buffering, VAD (Silero), turn detection (Smart Turn v3), and service adapters (Deepgram, Anthropic, Cartesia) without custom glue. The key tradeoff: Pipecat imports are heavy (~5–10s cold-start for VAD/ONNX models), requiring the `_warm_pipecat_models()` call at startup.

**Pending-calls bridge (`app.state.pending_calls`):**
Twilio's two-step call setup (POST `/twilio/voice` → separate WebSocket `/twilio/stream`) means client context must be threaded from HTTP to WebSocket via a shared dictionary keyed by `call_sid`. This is in-memory and single-machine; would break on multi-machine deploys.

**Model locked to Claude Haiku 4.5 on the call path:**
Sonnet/Opus would cost 3–5× more per call at the same call volume. Hardcoded at `RECEPTIONIST_MODEL = "claude-haiku-4-5-20251001"` in `voice_pipeline.py`. Demo and digest paths use the same constant via separate imports.

**Per-client routing by `To` Twilio number:**
`app.state.clients` is a `dict[twilio_number, ClientContext]`. Each inbound call's `To` field determines which client owns it, no URL prefix routing, no subdomains. Adding a client requires provisioning a Twilio number and adding a JSON file in `config/clients/`.

**Minute ceiling enforced in application code:**
Deepgram, Cartesia, and Fly have no hard billing caps. The minute ceiling check at `/twilio/voice` entry (before returning streaming TwiML) is the only hard stop. The counter is in-memory and resets on deploy, not suitable for monthly billing cycle tracking without a deploy-aligned reset cadence.

**Prompt caching (`enable_prompt_caching=True`):**
The Anthropic client sends the system prompt with `cache_control: ephemeral` to reduce cost for long calls with repeated context. The demo `/text-chat` path does the same.

**SQLite on Fly volume for transcripts:**
Single-machine assumption; connection pool is module-level dict keyed by path. Adequate at current scale. The module documents the migration path to Fly Postgres when multi-machine is needed.

**GDPR/PIPA compliance modes per client:**
`ClientConfig.gdpr_mode` and `pipa_mode` (mutually exclusive) inject different prompt prefixes and change tool availability. PIPA mode uses a hardcoded deterministic TTS greeting before the LLM's first turn, enforced by `MuteUntilFirstBotCompleteUserMuteStrategy`.

## Known Architectural Debt

**In-memory state is single-machine only:**
`app.state.clients`, `app.state.pending_calls`, `app.state.minute_counters`, `app.state.voicemail_pending`, `app.state.voicemail_transcripts`, and the demo token store (`_demo_tokens`) are all in-process dicts. Fly is currently configured with `min_machines_running = 1` and `auto_stop_machines = "off"` to prevent machine cycling, but any multi-machine or auto-scaling scenario would split this state invisibly.

**`pending_calls` race condition under scale:**
The `pending_calls` bridge (HTTP → WebSocket) is a dict pop in `twilio_stream`. If the WebSocket arrives before the POST handler writes the entry (unlikely but possible under load), the call drops with a `no pending_calls entry` log warning. No retry or locking.

**Voicemail client dispatch is hardcoded to first live client:**
The recording/transcription callbacks at `/twilio/voicemail/recording` and `/twilio/voicemail/transcription` lack a `To` header from Twilio, so they can't be per-client routed. Currently resolves to `next(ctx for ctx in clients.values() if ctx.config.live)`. Multi-client voicemail would need distinct callback URLs per client registered in Twilio.

**Minute counters reset on every deploy:**
Monthly billing cycle tracking would require persisting the counter to the SQLite volume between deploys. Currently a deploy = reset, meaning usage mid-period is erased on redeploy.

**Demo token store is unbounded in-memory:**
`_demo_tokens` in `main.py` grows indefinitely until a process restart. No eviction of expired-but-not-validated tokens. Low risk at current demo traffic but a memory leak under sustained load.

**`text_chat_handler.py` uses XML tag stripping as tool detection:**
The demo surface uses a regex-based `<log_request>` / `<end_call>` tag-stripping approach rather than Anthropic's structured tool-use API. This is a legacy pattern that diverges from the voice path's Pipecat function-tool schema and could silently fail if the model's format changes.

**`SheetsWriter` is synchronous (no `asyncio.to_thread`):**
`sheets_writer.append_row()` and `sheets_writer.read_all_rows()` are blocking Google API calls invoked directly in async handlers. `SmsNotifier` wraps its Twilio calls in `asyncio.to_thread`; `SheetsWriter` does not. Under concurrent calls this blocks the event loop briefly.

## Error Handling

**Strategy:** fail-safe for the call path, fail-loud for startup.

**Patterns:**
- Startup: `RuntimeError` raised and process exits if no client configs found, or (implicitly) if Anthropic client cannot be constructed. The `/healthz` endpoint returns 503 for missing credentials rather than 200.
- Call path: tool handler errors (`log_request`, `end_call`) are caught, logged, and returned as `{"status": "error"}` to the LLM, the call continues. `transcript_store.save()` swallows all exceptions so a broken DB never breaks a live call.
- SMS: `TwilioRestException` and generic `Exception` are caught, logged, return `False`. Never blocks call path.
- Pipeline: `except Exception: logger.exception()` in `run_voice_session`, pipeline errors are caught in the try/except around `runner.run()`, minute accounting still runs in `finally`.
- Unhandled FastAPI exceptions: caught by `@app.exception_handler(Exception)` → 500 JSON response.

## Cross-Cutting Concerns

**Logging:** JSON-structured (one object per line) via `tools/llm_proxy/logging_config.py`. `JsonFormatter` extracts `call_sid` as a top-level field from the message body. `LOG_FORMAT=text` for local dev. All call-path logs include `call_sid=...` for grep/jq filtering.

**Rate limiting:** `slowapi` middleware on `/text-chat` (30/hour per IP). No rate limiting on Twilio webhook endpoints (protected by HMAC signature validation instead).

**Security:**
- Twilio webhooks: HMAC-SHA1 signature validated per request via `twilio.request_validator.RequestValidator`. Bypass via `ALLOW_UNSIGNED_TWILIO=1` for local dev.
- Voicemail playback URLs: HMAC-SHA256 signed tokens with 30-day expiry.
- Demo auth: bearer tokens, 8-hour TTL, in-memory store.
- PII: Presidio + spaCy NER redaction before transcript persistence; only last-4 digits of phone numbers stored in Sheets.

**PII / privacy modes:**
- `gdpr_mode`: EU/UK/Swiss, default no-retain, opt-in recording, guaranteed human handoff, Article 9 data restraint
- `pipa_mode`: BC Canada, default retain with opt-out right, deterministic disclosure greeting
- Bare mode: default retain

---

*Architecture analysis: 2026-05-25*
