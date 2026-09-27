# Tech Stack

**Analysis Date:** 2026-05-25

## Runtime & Language

- **Python 3.12** (pinned in `infra/fly/Dockerfile`: `FROM python:3.12-slim`)
- Async throughout: `asyncio`-native pipeline via Pipecat; FastAPI route handlers are `async def`
- All CPU-bound work (PII redaction, Sheets writes, Twilio REST calls) dispatched via `asyncio.to_thread` to avoid blocking the event loop

## Web Framework

**FastAPI >= 0.115**, `tools/llm_proxy/main.py`

Key usage patterns:
- Single `_build_app()` factory function constructs and returns the `FastAPI` instance; module-level `app = _build_app()` is the uvicorn entry point
- `APIRouter` used to split voice routes (`tools/llm_proxy/voice_routes.py`) and voicemail routes (`tools/llm_proxy/voicemail.py`) from `main.py`
- WebSocket endpoint (`/twilio/stream`) served via FastAPI's native WebSocket support
- `CORSMiddleware` allows `clearslopedigital.com` and localhost dev origins
- `SlowAPIMiddleware` + `slowapi >= 0.1.9` enforces rate limits (30/hour on `/text-chat`)
- Per-call shared state stored on `app.state` (client registry, minute counters, pending voicemail events)

**Uvicorn >= 0.30**, ASGI server

- Dev: `python -m uvicorn tools.llm_proxy.main:app --host 0.0.0.0 --port 8080 --reload` (via `start.sh`)
- Production: run with `--proxy-headers --forwarded-allow-ips '*'` (required for Twilio signature validation behind Fly's HTTPS proxy)

**Starlette**, used directly for `StarletteJSONResponse` in the rate-limit error handler

## Key Dependencies

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | >=0.115 | HTTP + WebSocket framework; all routes |
| `uvicorn[standard]` | >=0.30 | ASGI server (WebSocket + HTTP/2 support) |
| `pipecat-ai[anthropic,deepgram,cartesia,silero,webrtc]` | >=0.0.50 | Real-time voice pipeline: VAD, STT, LLM, TTS wiring |
| `anthropic` | >=0.40 | Claude API client; both sync and async; prompt caching enabled |
| `twilio` | >=9.0 | Programmable Voice TwiML, Media Streams, REST API, request signature validation, SMS |
| `deepgram` | (via pipecat) | Streaming STT service |
| `cartesia` | (via pipecat) | Streaming TTS service |
| `silero` | (via pipecat) | On-device VAD (Voice Activity Detection), Silero ONNX model |
| `google-api-python-client` | >=2.140 | Google Sheets v4 API writes (booking intake, voicemail log) |
| `google-auth` | >=2.34 | Service account auth for Sheets |
| `google-auth-httplib2` | >=0.2 | HTTP transport for Google auth |
| `presidio-analyzer` | >=2.2 | PII entity detection in transcripts |
| `presidio-anonymizer` | >=2.2 | PII replacement/redaction in transcripts |
| `spacy` | >=3.7 | NLP backend for Presidio; `en_core_web_sm` model downloaded at Docker build time |
| `pydantic` | >=2.8 | Request/response models (`TextChatRequest`, `TextChatResponse`); config validation |
| `python-dotenv` | >=1.0 | Loads `.env` in dev; production reads from Fly secrets |
| `httpx` | >=0.27 | Async HTTP client; used in voicemail playback proxy to stream MP3 from Twilio |
| `boto3` | >=1.35 | AWS SDK, declared in requirements for S3/KMS recording storage; not currently active in the live call path (env vars `S3_BUCKET_RECORDINGS`, `KMS_KEY_ID` exist but no boto3 calls found in `tools/llm_proxy/`) |
| `pandas` | >=2.2 | Data processing in offline analysis tools (`tools/ingest_transcripts.py`, `tools/missed_revenue_audit.py`) |
| `pyyaml` | >=6.0 | Config/prompt file loading |
| `slowapi` | >=0.1.9 | Rate limiting middleware for FastAPI |
| `python-multipart` | >=0.0.20 | Multipart form parsing (Twilio POSTs form-encoded webhook payloads) |
| `sentry-sdk[fastapi]` | >=2.0 | Optional error monitoring; only initialized when `SENTRY_DSN` env var is set |
| `pytest` | >=8.3 | Test runner |
| `pytest-asyncio` | >=0.24 | Async test support |
| `ruff` | >=0.6 | Linting and formatting |

## Deployment

**Platform:** Fly.io (`infra/fly/fly.toml`, `infra/fly/Dockerfile`)

- App name: `clearslope-receptionist`
- Primary region: `sjc` (San Jose)
- VM: `shared-cpu-1x`, 1 GB RAM (app process); 256 MB RAM (retention process)
- Port: 8080 internal, HTTPS forced externally (`force_https = true`)
- `auto_stop_machines = "off"`, machine never sleeps; critical for keeping Pipecat VAD/turn models warm and avoiding Twilio 31921 cold-start disconnects
- `min_machines_running = 1`, always at least one instance live
- Concurrency: hard limit 25 connections, soft limit 20

**Two Fly processes:**
- `app`, runs the FastAPI/uvicorn server (`entrypoint.sh app`)
- `retention`, scheduled daily job that enforces transcript/recording retention policies (`entrypoint.sh retention`)

**Volume mount:**
- `transcripts_data` → `/data` (1 GB), persists `transcripts.db` (SQLite) across deploys

**Container:**
- Base: `python:3.12-slim`
- System packages: `build-essential`, `ffmpeg`, `libsndfile1`, `sqlite3`
- spaCy model `en_core_web_sm` downloaded at build time
- Entry point: `infra/fly/entrypoint.sh`

**Deploy command:** `fly deploy --config infra/fly/fly.toml`

## Dev Tooling

**Local dev server:** `./start.sh`
- Starts uvicorn with `--reload` on `$BACKEND_PORT` (default 8080)
- Polls `/healthz` to confirm readiness before showing the ready banner
- Requires `.env` and `.venv` to exist at project root

**Virtual environment:** `.venv/` (standard venv; `start.sh` references `.venv/bin/python`)

**Testing:**
- `pytest >= 8.3` + `pytest-asyncio >= 0.24`
- Test files not found in a dedicated `tests/` directory during exploration, co-located or absent for most modules
- Run with: `pytest`

**Linting/Formatting:** `ruff >= 0.6`
- No `.ruff.toml` or `pyproject.toml` found at project root; default config assumed
- Run with: `ruff check .` / `ruff format .`

**Environment config:** `.env` in dev (never committed); Fly secrets in production
- `.env.example` documents all required and optional variables

---

*Stack analysis: 2026-05-25*
