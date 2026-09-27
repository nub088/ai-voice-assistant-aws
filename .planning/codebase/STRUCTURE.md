<!-- refreshed: 2026-05-25 -->
# Codebase Structure

**Analysis Date:** 2026-05-25

## Directory Layout

```
AI Voice Assistant/
├── analyze_calls.py            # One-off: regex analysis of archived AllGreen transcripts
├── detailed_call_analysis.py   # One-off: extended transcript analysis
├── start.sh                    # Local dev launcher (uvicorn + healthcheck poll)
├── RUNBOOK.md                  # Operational procedures (deploy, onboard client, logs)
│
├── config/
│   └── clients/
│       ├── clearslope.json     # Live client: Clear Slope Pest Control
│       └── allgreen.json       # Template client: All Green Pest Control (live=false)
│
├── docs/
│   └── superpowers/
│       ├── plans/              # Planning documents
│       └── specs/              # Feature design specs (GDPR, PIPA, voicemail, etc.)
│
├── eval/
│   └── labeled_calls/
│       └── handled/            # Manually labeled call transcripts for eval
│
├── infra/
│   └── fly/
│       ├── Dockerfile          # Production container (Python 3.12, copies tools/ + config/)
│       ├── entrypoint.sh       # Process selector: "app" (uvicorn) or "retention" (daily job)
│       └── fly.toml            # Fly.io config: app=clearslope-receptionist, sjc, 1gb RAM
│
├── tests/
│   ├── replay_scenarios/       # Scenario JSON files for replay harness
│   └── test_*.py               # pytest test files
│
├── tools/
│   ├── __init__.py
│   ├── cpu_sampler.py          # CPU profiling utility
│   ├── data_subject_request.py # GDPR/PIPA data subject request handler
│   ├── extract_sales_reps.py   # One-off: extract sales rep data from transcripts
│   ├── ingest_transcripts.py   # Ingest raw transcripts into analysis format
│   ├── missed_revenue_audit.py # Audit missed revenue from AllGreen data
│   ├── redact_pii.py           # Presidio + spaCy PII redaction pipeline
│   ├── replay_transcript.py    # Test harness: replay a scenario through the pipeline
│   ├── retention_enforcer.py   # Daily: prune old transcripts from SQLite
│   ├── run_daily_retention.py  # Entrypoint for retention process
│   ├── text_chat_with_agent.py # CLI for testing /text-chat endpoint
│   ├── twilio_call_diagnostics.py # Diagnose call drop issues via Twilio API
│   ├── voicemail_retention.py  # Prune old voicemail rows from Sheets
│   ├── ws_sampler.py           # WebSocket timing sampler
│   │
│   ├── appscript/              # Google Apps Script files (legacy/reference)
│   ├── config/                 # Legacy config (superseded by config/clients/)
│   ├── prompts/
│   │   ├── receptionist_v0.3_data_driven.md  # Active system prompt (Pipecat path)
│   │   ├── receptionist_vapi_v1.md           # Legacy Vapi prompt (reference)
│   │   ├── pest_control_v1.md                # Legacy prompt (reference)
│   │   └── pest_taxonomy.json                # Canonical pest species taxonomy
│   │
│   └── llm_proxy/              # THE PRODUCTION APPLICATION
│       ├── __init__.py
│       ├── main.py             # FastAPI app factory, startup, all route registration
│       ├── voice_routes.py     # POST /twilio/voice + WS /twilio/stream
│       ├── voice_pipeline.py   # Pipecat pipeline factory + run_voice_session()
│       ├── function_tools.py   # LLM tool schemas + per-call handler closures
│       ├── client_config.py    # ClientConfig, ClientContext dataclasses + JSON loader
│       ├── prompt_loader.py    # Prompt + taxonomy loading, interpolation, time_context()
│       ├── sheets_writer.py    # Google Sheets append/read (booking + voicemail tabs)
│       ├── sms_notifier.py     # Twilio SMS: customer confirmation + owner alerts
│       ├── voicemail.py        # Voicemail TwiML, callbacks, playback proxy, handler factory
│       ├── voicemail_urls.py   # HMAC-signed playback URL generation + verification
│       ├── transcript_store.py # SQLite WAL store for redacted transcripts at /data/
│       ├── text_chat_handler.py # Stateless /text-chat handler (demo surface)
│       ├── digest.py           # Weekly digest computation (pure, no I/O)
│       ├── digest_cli.py       # CLI entrypoint for digest; writes WeeklyDigest sheet tab
│       └── logging_config.py   # JSON-structured logging setup (JsonFormatter)
│
├── .planning/
│   ├── PROJECT.md              # Product overview, requirements, key decisions
│   └── codebase/               # Architecture maps (this directory)
│       ├── ARCHITECTURE.md
│       └── STRUCTURE.md
│
├── .tmp/
│   ├── logs/                   # Local uvicorn log output
│   ├── transcripts/            # Local transcript processing intermediates
│   ├── audit/                  # PII audit output
│   └── cpu_samples/            # CPU profiler output
│
└── .venv/                      # Python virtualenv (gitignored)
```

## Module Map

### `tools/llm_proxy/main.py`
- **Exports:** `app` (FastAPI instance, created by `_build_app()`)
- **Imports:** All `llm_proxy` submodules; `anthropic`, `fastapi`, `slowapi`
- **Key behavior:** Calls `scan_client_configs()` → builds `ClientContext` per client → stores in `app.state.clients`. Warms Pipecat VAD/ONNX models synchronously before binding. Registers voice and voicemail routers.

### `tools/llm_proxy/voice_routes.py`
- **Exports:** `router` (FastAPI `APIRouter` with `/twilio/voice` POST and `/twilio/stream` WS)
- **Imports:** `voice_pipeline.run_voice_session`, `twilio.request_validator`, `twilio.twiml`
- **Key behavior:** Client lookup by `To` number, Twilio signature validation, minute ceiling gate, `pending_calls` bridge write (POST) / pop (WS).

### `tools/llm_proxy/voice_pipeline.py`
- **Exports:** `run_voice_session()`, `RECEPTIONIST_MODEL`
- **Imports:** All pipecat services and transport; `function_tools.make_handlers`, `prompt_loader.time_context`, `transcript_store`
- **Key behavior:** Builds and runs Pipecat pipeline per call. Handles PIPA/GDPR/bare greeting modes. `finally` block: minute accounting + transcript persistence.

### `tools/llm_proxy/function_tools.py`
- **Exports:** `TOOLS_SCHEMA` (ToolsSchema), `make_handlers()`
- **Imports:** `sheets_writer.SheetsWriter`, `sms_notifier.SmsNotifier`, `voicemail.make_transfer_to_voicemail_handler`
- **Key behavior:** `make_handlers()` returns a dict of async callables keyed by tool name, each closing over `call_sid`, `sheets_writer`, `sms_notifier`. Returns `_recording_state` dict as a hidden entry for the pipeline's `finally` block to read.

### `tools/llm_proxy/client_config.py`
- **Exports:** `ClientConfig`, `ClientContext`, `BusinessIdentity`, `BusinessHours`, `PromptPaths`, `load_client_config()`, `scan_client_configs()`, `interpolate()`
- **Imports:** `zoneinfo`, stdlib only
- **Key behavior:** Parses `config/clients/*.json`; raises `ValueError` on duplicate `twilio_number` or conflicting `gdpr_mode`+`pipa_mode`.

### `tools/llm_proxy/prompt_loader.py`
- **Exports:** `LoadedPrompt`, `load_prompt()`, `time_context()`
- **Imports:** `client_config.interpolate`
- **Key behavior:** Composes `{prompt_md}\n\n```json\n{taxonomy}\n````, interpolates `{{business_name}}` etc., SHA256-hashes for version tag. `time_context()` returns a single-line current time + business-hours status string injected at call start.

### `tools/llm_proxy/sheets_writer.py`
- **Exports:** `SheetsWriter`, `format_row()`, `writer_from_env()`, `HEADER_ROW`, `VOICEMAIL_HEADER`, `VOICEMAIL_TAB_NAME`
- **Imports:** `google.oauth2.service_account`, `googleapiclient.discovery`
- **Key behavior:** Synchronous Google Sheets v4 API wrapper. Methods: `append_row()`, `read_all_rows()`, `ensure_tab()`, `append_to_tab()`, `read_voicemail_rows()`, `delete_voicemail_row()`, `update_voicemail_transcript()`. Sheet credentials: `GOOGLE_SERVICE_ACCOUNT_FILE` (global) + `{PREFIX}_GOOGLE_SHEET_ID` (per-client).

### `tools/llm_proxy/sms_notifier.py`
- **Exports:** `SmsNotifier`, `notifier_from_env()`
- **Imports:** `twilio.rest`, `asyncio.to_thread`
- **Key behavior:** Three async send methods: `send_customer_confirmation()`, `send_owner_emergency_alert()`, `send_voicemail_alert()`. All dispatch Twilio REST to a thread. Anonymous callers silently skipped.

### `tools/llm_proxy/voicemail.py`
- **Exports:** `router` (APIRouter with 4 routes), `make_transfer_to_voicemail_handler()`, `handle_recording_callback()`, `handle_transcription_callback()`
- **Imports:** `httpx`, `twilio.rest`, `voicemail_urls`, `sheets_writer`
- **Key behavior:** `make_transfer_to_voicemail_handler()` returns a Pipecat tool handler that calls `twilio.calls.update` to redirect the live call. Recording callback spawns a background task (held in `_VOICEMAIL_BG_TASKS` to prevent GC) that waits up to 60s for transcription.

### `tools/llm_proxy/voicemail_urls.py`
- **Exports:** `sign_playback_url()`, `verify_playback_token()`, `InvalidToken`
- **Imports:** `hmac`, `hashlib`, `base64`, stdlib only
- **Key behavior:** HMAC-SHA256 over `"{recording_sid}:{exp}"`. Verification is constant-time.

### `tools/llm_proxy/transcript_store.py`
- **Exports:** `save()`
- **Imports:** `sqlite3`, `threading`
- **Key behavior:** One cached connection per DB path; WAL mode; `INSERT OR IGNORE` for idempotency. All exceptions swallowed. DB path: `TRANSCRIPT_DB_PATH` env or `/data/transcripts.db`.

### `tools/llm_proxy/text_chat_handler.py`
- **Exports:** `handle_text_chat()`, `TextChatRequest`, `TextChatResponse`
- **Imports:** `anthropic`, `pydantic`
- **Key behavior:** Calls `client.messages.create()` with cached system prompt; strips `<log_request>` / `<end_call>` XML tags from response. Detects call end via `END_CALL_RE`.

### `tools/llm_proxy/digest.py`
- **Exports:** `CallRow`, `Digest`, `parse_rows()`, `compute_digest()`, `render_digest_text()`, `render_digest_row()`, `DIGEST_HEADER_ROW`, `DIGEST_TAB_NAME`
- **Imports:** stdlib only
- **Key behavior:** Pure functions; no I/O. `parse_rows()` is tolerant of ragged Sheet rows. Module-level `assert` guards row-schema/header alignment.

### `tools/llm_proxy/logging_config.py`
- **Exports:** `setup_logging()`
- **Imports:** `logging`, stdlib
- **Key behavior:** Installs `JsonFormatter` on root logger. Extracts `call_sid` as a top-level field via regex.

### `tools/redact_pii.py`
- **Exports:** `PIIRedactor`, `load_default_redactor()`
- **Imports:** `presidio_analyzer`, `presidio_anonymizer`, `spacy`
- **Key behavior:** Three-pass pipeline: Presidio NER → optional Claude contextual pass → audit residuals. Loaded lazily in `voice_pipeline._get_redactor()`.

## Entry Points

**Production (Fly.io):**
- `infra/fly/entrypoint.sh app` → `uvicorn tools.llm_proxy.main:app --host 0.0.0.0 --port 8080`
- `infra/fly/entrypoint.sh retention` → `python -m tools.run_daily_retention` (daily Fly machine schedule)

**Local development:**
- `./start.sh` → same uvicorn command with `--reload`, default port 8080

**Main routes:**
| Method | Path | Handler | Purpose |
|--------|------|---------|---------|
| GET | `/healthz` | `main.py` | Dependency check (returns 503 if any required key missing) |
| POST | `/auth/demo` | `main.py` | Issue 8-hour demo bearer token |
| POST | `/text-chat` | `main.py` → `text_chat_handler` | Demo widget chat |
| POST | `/twilio/voice` | `voice_routes.py` | Inbound call → TwiML Media Stream |
| WS | `/twilio/stream` | `voice_routes.py` | Audio stream → Pipecat pipeline |
| POST | `/twilio/voicemail.xml` | `voicemail.py` | Record verb TwiML |
| POST | `/twilio/voicemail/recording` | `voicemail.py` | Twilio recording-status callback |
| POST | `/twilio/voicemail/transcription` | `voicemail.py` | Twilio transcription callback |
| GET | `/vm/{recording_sid}` | `voicemail.py` | Signed MP3 playback proxy |

## Configuration

### Flow: `.env` / Fly secrets → `main.py` → `ClientContext`

**Global secrets (shared across all clients):**
| Variable | Used by | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | `voice_pipeline.py`, `text_chat_handler.py` | Claude API |
| `DEEPGRAM_API_KEY` | `voice_pipeline.py` | STT |
| `CARTESIA_API_KEY` | `voice_pipeline.py` | TTS |
| `CARTESIA_VOICE_ID` | `voice_pipeline.py` | TTS voice selection |
| `TWILIO_ACCOUNT_SID` | `sms_notifier.py`, `voicemail.py` | Twilio REST client |
| `TWILIO_AUTH_TOKEN` | `voice_routes.py`, `voicemail.py`, `sms_notifier.py` | Webhook signature validation + SMS |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `sheets_writer.py` | Google Sheets service account (path to JSON file) |
| `VOICEMAIL_URL_SECRET` | `voicemail_urls.py`, `main.py` | HMAC key for signed playback URLs |
| `PUBLIC_BASE_URL` | `voice_routes.py`, `voicemail.py` | Canonical HTTPS URL (e.g. `https://clearslope-receptionist.fly.dev`) |
| `SENTRY_DSN` | `main.py` | Optional error tracking |
| `DEMO_USERNAME` / `DEMO_PASSWORD` | `main.py` | Demo auth credentials |
| `REDACTION_HMAC_KEY` | `tools/redact_pii.py` | Filename hashing for PII redaction |
| `TRANSCRIPT_DB_PATH` | `transcript_store.py` | Override SQLite path (default `/data/transcripts.db`) |

**Per-client secrets (prefixed by `env_prefix` from client JSON):**
| Variable | Example | Purpose |
|----------|---------|---------|
| `{PREFIX}_GOOGLE_SHEET_ID` | `CLEARSLOPE_GOOGLE_SHEET_ID` | Booking intake sheet |
| `{PREFIX}_TWILIO_SMS_FROM` | `CLEARSLOPE_TWILIO_SMS_FROM` | Outbound SMS number |
| `{PREFIX}_OWNER_NOTIFY_NUMBER` | `CLEARSLOPE_OWNER_NOTIFY_NUMBER` | Owner emergency alert target |

**Dev escape hatches:**
| Variable | Purpose |
|----------|---------|
| `ALLOW_UNSIGNED_TWILIO=1` | Skip Twilio signature validation (local dev / tunnels) |
| `LOG_LEVEL=DEBUG` | Verbose logging |
| `LOG_FORMAT=text` | Human-readable log format instead of JSON |
| `BACKEND_PORT` | Override uvicorn port (default 8080) |

### Client JSON schema: `config/clients/<client_id>.json`

```json
{
  "client_id": "clearslope",
  "twilio_number": "+1...",
  "env_prefix": "CLEARSLOPE",
  "minute_ceiling": 500,
  "live": true,
  "gdpr_mode": false,
  "pipa_mode": false,
  "business": {
    "name": "Clear Slope Pest Control",
    "short_name": "Clear Slope",
    "industry": "pest control and wildlife exclusion",
    "ai_name": "Sarah"
  },
  "hours": {
    "timezone": "America/Vancouver",
    "weekdays": [0,1,2,3,4],
    "open_hour": 8,
    "close_hour": 17
  },
  "prompt": {
    "path": "tools/prompts/receptionist_v0.3_data_driven.md",
    "taxonomy_path": "tools/prompts/pest_taxonomy.json"
  }
}
```

### What is hardcoded vs configurable

**Hardcoded:**
- LLM model: `RECEPTIONIST_MODEL = "claude-haiku-4-5-20251001"` in `voice_pipeline.py`
- Audio: μ-law 8 kHz (Twilio requirement)
- CORS origins: `clearslopedigital.com` + localhost in `main.py`
- Demo token TTL: 8 hours in `main.py`
- Voicemail recording max length: 120 seconds in `voicemail.py`
- Recording callback wait timeout: 60 seconds in `voicemail.py`
- Voicemail audio retention: 30 days (in signed URL expiry; actual Twilio-side retention may differ)
- Fly machine: `shared-cpu-1x`, 1 GB RAM, `sjc` region in `fly.toml`

**Configurable per-client:**
- Business identity (name, AI persona name, industry)
- Business hours + timezone
- System prompt + pest taxonomy file paths
- Minute ceiling
- Google Sheet ID
- SMS from/to numbers
- GDPR / PIPA compliance mode
- `live` flag (excludes from `/healthz` readiness)

## Where to Add New Code

**New client tenant:**
- Add `config/clients/<client_id>.json` (copy from `config/clients/clearslope.json`)
- Set prefixed Fly secrets: `{PREFIX}_GOOGLE_SHEET_ID`, `{PREFIX}_TWILIO_SMS_FROM`, `{PREFIX}_OWNER_NOTIFY_NUMBER`
- No code changes required

**New LLM function tool:**
- Add `FunctionSchema` in `tools/llm_proxy/function_tools.py` and add to `TOOLS_SCHEMA`
- Add async handler inside `make_handlers()` in the same file
- Add to `return` dict of `make_handlers()` (or as `_internal_key` if not an LLM tool)

**New FastAPI endpoint:**
- If voice/Twilio-related: add to `tools/llm_proxy/voice_routes.py` (via `router`)
- If voicemail-related: add to `tools/llm_proxy/voicemail.py` (via `router`)
- If app-level (auth, health, demo): add directly in `_build_app()` in `tools/llm_proxy/main.py`

**New per-client config field:**
- Add field to `ClientConfig` dataclass in `tools/llm_proxy/client_config.py`
- Add parsing in `load_client_config()` in the same file
- Update all `config/clients/*.json` files

**New system prompt version:**
- Add Markdown file to `tools/prompts/`
- Update `prompt.path` in the relevant `config/clients/*.json`
- No code changes required; `load_prompt()` uses the path from config

**New one-off analysis or admin script:**
- Add to `tools/` (top level, not `llm_proxy/`)
- Run manually or as a Fly cron process via `entrypoint.sh`

**Tests:**
- Add test files to `tests/`
- Replay scenarios: add JSON to `tests/replay_scenarios/`
- Run with `pytest` from project root

---

*Structure analysis: 2026-05-25*
