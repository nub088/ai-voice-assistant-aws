# External Integrations

**Analysis Date:** 2026-05-25

---

## Anthropic (Claude)

- **Purpose**: LLM backbone for both the real-time voice receptionist and the text-based demo widget. Handles all natural language understanding, triage, and structured tool-call generation.
- **Protocol**: REST (async SDK). Voice path uses `AnthropicLLMService` inside Pipecat; demo path uses `anthropic.AsyncAnthropic` directly.
- **Model**: `claude-haiku-4-5-20251001` locked on both the voice path and demo text-chat path. Prompt caching enabled on all calls (`enable_prompt_caching=True`; system prompt sent with `cache_control: ephemeral`).
- **Auth**: `ANTHROPIC_API_KEY` environment variable. Warn-on-missing at startup; calls fail without it.
- **Files**:
  - `tools/llm_proxy/voice_pipeline.py`, `AnthropicLLMService` instantiation, model constant `RECEPTIONIST_MODEL`
  - `tools/llm_proxy/text_chat_handler.py`, `AsyncAnthropic` client, `handle_text_chat()`
  - `tools/llm_proxy/main.py`, client construction, startup validation
- **Criticality**: Core, all voice and demo paths fail without this.

---

## Twilio, Programmable Voice + Media Streams

- **Purpose**: Inbound phone call reception. Twilio routes PSTN calls to the app, opens a WebSocket (Media Streams) carrying μ-law 8 kHz audio, and carries TwiML responses back to control call flow (connect, hang up, redirect to voicemail).
- **Protocol**:
  - Webhook: Twilio POSTs form-encoded data to `POST /twilio/voice` when a call arrives.
  - WebSocket: Twilio opens `WS /twilio/stream` and streams bidirectional audio frames.
  - REST API: `TwilioClient.calls(call_sid).update()` used to redirect a live call into voicemail.
  - All inbound webhooks validated via HMAC-SHA1 (`twilio.request_validator.RequestValidator`).
- **Auth**:
  - `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN`, global credentials used for REST client and signature validation.
  - Dev escape hatch: `ALLOW_UNSIGNED_TWILIO=1` skips signature checks locally.
  - `PUBLIC_BASE_URL` must match the URL Twilio signs (set to `<app>.fly.dev` on Fly).
- **Files**:
  - `tools/llm_proxy/voice_routes.py`, `POST /twilio/voice`, `WS /twilio/stream`, signature validation
  - `tools/llm_proxy/voice_pipeline.py`, `TwilioFrameSerializer`, μ-law 8 kHz audio serialization
  - `tools/llm_proxy/voicemail.py`, `POST /twilio/voicemail.xml`, recording/transcription callbacks, REST redirect
  - `tools/llm_proxy/sms_notifier.py`, `TwilioClient` for SMS
  - `tools/llm_proxy/main.py`, auth token loading, client registry wiring
- **Criticality**: Core, the entire voice path depends on Twilio.

---

## Twilio, SMS Notifications

- **Purpose**: Two post-call SMS channels: (1) customer confirmation sent to the caller after a booking is logged; (2) owner emergency alert sent to the business owner when urgency is `emergency` or a human handoff is requested. Also sends voicemail alerts with a signed playback URL.
- **Protocol**: Twilio REST API, synchronous `client.messages.create()` dispatched via `asyncio.to_thread`.
- **Auth**: Shared `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN`. Per-client from-number: `{PREFIX}_TWILIO_SMS_FROM`. Per-client owner destination: `{PREFIX}_OWNER_NOTIFY_NUMBER`.
- **Files**:
  - `tools/llm_proxy/sms_notifier.py`, `SmsNotifier` class, all three send methods
  - `tools/llm_proxy/function_tools.py`, invokes `send_customer_confirmation`, `send_owner_emergency_alert`
  - `tools/llm_proxy/voicemail.py`, invokes `send_voicemail_alert`
  - `tools/llm_proxy/main.py`, `notifier_from_env()` per-client instantiation
- **Criticality**: Supporting, SMS failures are caught and logged; the call path continues.

---

## Deepgram, Streaming STT

- **Purpose**: Real-time speech-to-text transcription of caller audio during live calls. Streams audio from Twilio to Deepgram and returns text frames to the Pipecat pipeline.
- **Protocol**: WebSocket (managed internally by Pipecat's `DeepgramSTTService`).
- **Auth**: `DEEPGRAM_API_KEY` environment variable. Warn-on-missing at startup; voice calls rejected without it.
- **Files**:
  - `tools/llm_proxy/voice_pipeline.py`, `DeepgramSTTService(api_key=deepgram_api_key)`
- **Criticality**: Core, voice path fails without this.

---

## Cartesia, Streaming TTS

- **Purpose**: Real-time text-to-speech synthesis. Converts Claude's text responses into audio streamed back to the caller via Twilio.
- **Protocol**: WebSocket (managed internally by Pipecat's `CartesiaTTSService`). Output sample rate forced to 8000 Hz to match Twilio Media Streams.
- **Auth**: `CARTESIA_API_KEY` + `CARTESIA_VOICE_ID` environment variables. Warn-on-missing at startup; voice calls rejected without both.
- **Files**:
  - `tools/llm_proxy/voice_pipeline.py`, `CartesiaTTSService(api_key=..., voice_id=..., sample_rate=8000)`
- **Criticality**: Core, voice path fails without this.

---

## Google Sheets, Booking Intake + Voicemail Log

- **Purpose**: Persistent, human-readable record of every booking intake and voicemail. The Claude model calls the `log_request` function tool; the handler appends a row to the client's Google Sheet. A separate "Voicemails" tab tracks voicemail metadata and transcripts.
- **Protocol**: Google Sheets REST API v4 via `google-api-python-client`. Synchronous; called from async handlers via `asyncio.to_thread` is NOT done here, `SheetsWriter` methods are called directly from async handlers, which means they block the event loop momentarily. (See CONCERNS.md.)
- **Auth**:
  - Service account JSON file: `GOOGLE_SERVICE_ACCOUNT_FILE` (path to JSON, default `./google-service-account.json`).
  - Per-client sheet ID: `{PREFIX}_GOOGLE_SHEET_ID`.
  - The sheet must be shared with the service account email with Editor permission.
- **Files**:
  - `tools/llm_proxy/sheets_writer.py`, `SheetsWriter` class, `writer_from_env()`, all CRUD operations
  - `tools/llm_proxy/function_tools.py`, `log_request` handler calls `sheets_writer.append_row()`
  - `tools/llm_proxy/voicemail.py`, appends and updates voicemail rows
  - `tools/llm_proxy/main.py`, `writer_from_env()` per-client instantiation; healthz check
- **Criticality**: Core for live clients, healthz returns 503 if a live client has no sheets writer configured.

---

## Pipecat, Real-Time Voice Pipeline

- **Purpose**: Orchestration framework that wires together Deepgram STT → Claude LLM → Cartesia TTS with VAD (Silero) into a single streaming pipeline per call. Handles turn detection, audio serialization, and function-tool dispatch.
- **Protocol**: Internal async pipeline framework; no external network calls from Pipecat itself beyond what each service (Deepgram, Cartesia, Anthropic) makes.
- **Key models loaded at startup** (blocking warm-up in `main._warm_pipecat_models`):
  - `SileroVADAnalyzer`, on-device ONNX VAD model
  - `LocalSmartTurnAnalyzerV3`, on-device turn-end detection
  - Both held on `app.state` to keep ONNX runtime warm across calls (~100ms per-call construction vs ~5s cold start)
- **Auth**: None (local library).
- **Files**:
  - `tools/llm_proxy/voice_pipeline.py`, full pipeline construction, `run_voice_session()`
  - `tools/llm_proxy/main.py`, `_warm_pipecat_models()` startup hook
- **Criticality**: Core, all voice calls run through Pipecat.

---

## SQLite, Transcript Store

- **Purpose**: Durable, per-call storage of PII-redacted call transcripts. Written at end-of-call; respects caller opt-out (PIPA/GDPR modes). The DB lives on a persistent Fly volume.
- **Protocol**: Local file I/O via Python `sqlite3` stdlib. WAL mode enabled for concurrent reads.
- **Auth**: None (local file). Path: `/data/transcripts.db` (Fly volume); overridable via `TRANSCRIPT_DB_PATH` env var.
- **Files**:
  - `tools/llm_proxy/transcript_store.py`, `save()`, schema, connection cache, write lock
  - `tools/llm_proxy/voice_pipeline.py`, calls `transcript_store.save()` in the post-call `finally` block
- **Criticality**: Supporting, failures are swallowed with logging; the call path is never interrupted.

---

## Presidio + spaCy, PII Redaction

- **Purpose**: Before any transcript is logged to SQLite or emitted as a structured log line, Presidio scans the text and replaces PII entities (names, phone numbers, addresses, etc.) with placeholder tokens. HMAC-based filename hashing for data subject access requests.
- **Protocol**: Local library call (no network). `en_core_web_sm` spaCy model loaded at first use (lazy init, cached process-wide).
- **Auth**: `REDACTION_HMAC_KEY`, 32-byte hex key for deterministic filename HMACs. Without this, `_get_redactor()` returns `None` and transcripts are not persisted.
- **Files**:
  - `tools/redact_pii.py`, `PIIRedactor`, `load_default_redactor()`, `RedactionResult`
  - `tools/llm_proxy/voice_pipeline.py`, `_get_redactor()`, `_persist_redacted_transcript()`
  - `tools/ingest_transcripts.py`, offline batch redaction of legacy transcripts
- **Criticality**: Supporting, if redactor is unavailable, transcripts are not persisted (logged as warning). Call path continues.

---

## Google Apps Script, Booking Intake Mirror

- **Purpose**: A secondary intake mirror; the app can POST booking data to an Apps Script web-app endpoint for alternate processing or legacy Google Form compatibility.
- **Protocol**: REST (HTTPS POST to `APPS_SCRIPT_WEB_APP_URL`).
- **Auth**: `APPS_SCRIPT_WEB_APP_URL`, no additional auth token documented; Apps Script web-app URLs are access-controlled at the Google level.
- **Files**: Referenced in `.env.example`; no call sites found in `tools/llm_proxy/` during exploration, may be used by offline scripts or be a planned/legacy integration.
- **Criticality**: Optional, not part of the live call path.

---

## AWS S3 + KMS, Recording Storage (Declared, Not Active)

- **Purpose**: Encrypted storage of call recordings. `boto3 >= 1.35`, `S3_BUCKET_RECORDINGS`, `S3_BUCKET_TRANSCRIPTS_RAW`, and `KMS_KEY_ID` are declared in `requirements.txt` and `.env.example` but no `boto3` calls were found in `tools/llm_proxy/` during exploration. Likely a planned or partially-built integration.
- **Protocol**: AWS SDK (boto3).
- **Auth**: `AWS_PROFILE=ai-receptionist` or `AWS_REGION=ca-central-1` + standard AWS credential chain. `S3_BUCKET_RECORDINGS`, `KMS_KEY_ID` populated after Terraform apply.
- **Files**: `tools/ingest_transcripts.py` may use it for offline batch work; not wired to the live call path.
- **Criticality**: Optional, not active in the live call path as of this analysis.

---

## Sentry, Error Monitoring

- **Purpose**: Optional exception tracking and performance monitoring. Only initialized when `SENTRY_DSN` is set; entirely absent if the env var is unset or the SDK is not installed.
- **Protocol**: HTTPS (Sentry SDK handles transport).
- **Auth**: `SENTRY_DSN`, DSN URL. `SENTRY_ENV` (default `production`), `SENTRY_TRACES_SAMPLE_RATE` (default `0.0`).
- **Files**:
  - `tools/llm_proxy/main.py`, `_init_sentry()` called at app build time
- **Criticality**: Optional, app starts normally without it.

---

## Fly.io, Hosting & Infrastructure

- **Purpose**: Production deployment platform. Provides HTTPS termination, persistent volume storage for the SQLite transcript DB, scheduled process execution (daily retention job), and health-check probing.
- **Protocol**: Fly platform (not a runtime API call from the app itself).
- **Auth**: Fly deploy token / `fly` CLI.
- **Files**:
  - `infra/fly/fly.toml`, app config, VM sizing, volume mounts, process definitions
  - `infra/fly/Dockerfile`, container build
  - `infra/fly/entrypoint.sh`, process dispatcher (`app` vs `retention`)
- **Criticality**: Core (production infrastructure).

---

*Integration audit: 2026-05-25*
