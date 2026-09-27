# Code Conventions

**Analysis Date:** 2026-05-25

## Style & Formatting

**Formatter/Linter:** Ruff (declared in `requirements.txt` as `ruff>=0.6`). No `ruff.toml` or `pyproject.toml` found at the repo root, Ruff is run in default mode, meaning:
- Line length: 88 (Ruff default)
- No enforced import ordering config detected; imports follow stdlib → third-party → local pattern consistently in source

**Observed patterns:**
- Every source file in `tools/llm_proxy/` starts with `from __future__ import annotations` (15 of ~16 files do)
- `from typing import Any`, `TYPE_CHECKING`, and `Callable` are used throughout; `Optional` is avoided in favour of `X | None`
- Type hints are consistently present on all public functions and methods

## Naming Conventions

**Files:**
- `snake_case.py` throughout, `client_config.py`, `voice_pipeline.py`, `transcript_store.py`
- Test files: `test_<module_name>.py` e.g. `test_redact_pii.py`, `test_proxy_app.py`

**Functions:**
- Public: `snake_case`, `load_client_config()`, `run_voice_session()`, `make_handlers()`
- Private (module-internal): `_snake_case` prefix, `_build_app()`, `_warm_pipecat_models()`, `_get_redactor()`, `_redact_payload_for_log()`
- Private methods on classes: `_snake_case`, `_contextual_pass()`, `_audit()`, `_build_operators()`

**Variables:**
- `snake_case` universally
- Constants: `UPPER_SNAKE_CASE`, `RECEPTIONIST_MODEL`, `TWILIO_SAMPLE_RATE`, `DEFAULT_ENTITIES`, `GDPR_PROMPT_PREFIX`
- Module-level singletons: `UPPER_SNAKE_CASE` or `_UPPER_SNAKE_CASE`, `_REDACTOR`, `_REDACTOR_INITIALIZED`

**Classes:**
- `PascalCase`, `PIIRedactor`, `ClientConfig`, `BusinessIdentity`, `JsonFormatter`
- Dataclasses used extensively for config/data objects (`@dataclass(frozen=True)` for immutable config)

**Environment variable naming:**
- Global: bare `UPPERCASE`, `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`
- Per-client (multi-tenant): `{PREFIX}_{KEY}`, `CLEARSLOPE_GOOGLE_SHEET_ID`, `ALLGREEN_TWILIO_SMS_FROM`
- Prefixes are uppercase client IDs defined in `config/clients/*.json` via `env_prefix` field

## Error Handling

**Pattern: structured logging before returning an error result.** Handlers never raise into the Pipecat call path, they catch, log, and return an `{"status": "error", "message": ...}` dict. Example from `tools/llm_proxy/function_tools.py`:

```python
try:
    row = format_row(args, vapi_call_id=call_sid, ...)
    sheets_writer.append_row(row)
except Exception as exc:
    safe_args = _redact_payload_for_log(args)
    logger.error("call_sid=%s log_request DROPPED row=%s", call_sid, payload)
    logger.exception("call_sid=%s log_request failed", call_sid)
    await params.result_callback({"status": "error", "message": str(exc)})
    return
```

**Pattern: best-effort blocks with explicit `logger.exception()`** for non-critical paths:

```python
try:
    result = redactor.redact_text(raw)
except Exception:
    logger.exception("call_sid=%s redaction failed; transcript not persisted", call_sid)
    return
```

**Global exception handler** in `tools/llm_proxy/main.py` catches all unhandled FastAPI exceptions and returns `{"error": "internal error"}` with 500, ensuring nothing leaks stack traces to clients.

**Sentinel values:** `None` guards are used consistently for optional resources (e.g. `sheets_writer is None`, `sms_notifier is None`) before dispatching, checked at call time, not at startup.

**`raise ... from exc`** used when wrapping `ValueError` to preserve the exception chain (seen in `tools/redact_pii.py`, `tools/retention_enforcer.py`).

## Async Patterns

**Framework:** FastAPI with `async def` route handlers; Pipecat pipeline runs inside `await runner.run(task)`.

**All Twilio webhook handlers are `async def`**; Google Sheets writes are synchronous (blocking), the Sheets API client is not wrapped in `asyncio.to_thread`. This is a known trade-off (Sheets calls are infrequent).

**Exception: SMS notifier** wraps Twilio's synchronous `messages.create` in `asyncio.to_thread` (`tools/llm_proxy/sms_notifier.py`).

**Concurrent SMS dispatch** uses `asyncio.gather(*sends, return_exceptions=True)`, fire-and-forget pattern; failures are swallowed at the gather level, `return_exceptions=True` prevents a single send failure from cancelling others.

**Background tasks** for voicemail: `asyncio.create_task()` is used in `tools/llm_proxy/voicemail.py` to schedule the 60-second wait for transcription. Cancellable via `asyncio.Event`.

**WebSocket handling** in `tools/llm_proxy/voice_routes.py`: the Pipecat `FastAPIWebsocketTransport` wraps the raw WebSocket, routes do not interact with the WebSocket frames directly.

**Module-level lazy init** for the PII redactor (expensive to load): a `_REDACTOR_INITIALIZED` sentinel bool and `_REDACTOR` module variable are used to cache both success and failure, avoiding repeated expensive imports (`tools/llm_proxy/voice_pipeline.py` lines 225–248).

## Logging

**Framework:** stdlib `logging`. A custom `JsonFormatter` is configured at startup via `setup_logging()` in `tools/llm_proxy/logging_config.py`.

**Format:** JSON to stdout in production (`LOG_FORMAT=json`, default). Controlled by `LOG_FORMAT` env var; `text` format available for local dev. `LOG_LEVEL` env var controls level (default `INFO`).

**JSON payload shape:**
```json
{"ts": "<iso8601>", "level": "INFO", "logger": "voice_pipeline", "msg": "...", "call_sid": "CA..."}
```
`call_sid` is extracted from the message string via regex and promoted to a top-level field for grep/jq querying.

**Logger naming:** module-level `logger = logging.getLogger("<module_name>")`, each module has its own logger named after it: `"llm_proxy"`, `"voice_pipeline"`, `"function_tools"`, `"retention_enforcer"`.

**What gets logged:**
- `INFO`: every HTTP request/response (`REQ`/`RES` lines), client startup, model warm-up timing, call lifecycle events (`call_sid=...`), `log_request` results, `REDACTED_TRANSCRIPT` at end-of-call
- `WARNING`: missing optional env vars, configuration gaps (no sheets writer, no SMS notifier), signature validation absent
- `ERROR`: dropped rows, missing required keys mid-call
- `exception()` (ERROR + traceback): redaction failures, pipeline errors, unhandled exceptions
- Special prefix `QUARANTINE_REDACTED_TRANSCRIPT`: transcripts with PII audit residuals, queryable separately

**Log redaction discipline:** Full phone numbers are never logged. The `_redact_payload_for_log()` helper (in `tools/llm_proxy/function_tools.py`) replaces callback numbers with `***{last4}` and truncates addresses before they reach any log line.

## Configuration Management

**Loading:** `python-dotenv` via `load_dotenv()` at the top of `tools/llm_proxy/main.py`. Called once at app build time; tests use `monkeypatch.setenv/delenv` to override.

**Access pattern:** `os.environ.get("VAR_NAME", "").strip()`, always `.strip()` on string secrets to guard against whitespace in `.env` files. Boolean flags: `os.environ.get("FLAG", "").lower() in {"1", "true", "yes"}`.

**Structured config:** Per-client config is JSON-based, not env-var-based. Each client has a JSON file in `config/clients/<client_id>.json` loaded at startup via `scan_client_configs()` in `tools/llm_proxy/client_config.py`. The `ClientConfig` dataclass is `frozen=True` (immutable after load).

**Multi-tenant env prefix:** Per-client env vars use `{cfg.env_prefix}_{VAR}` pattern, e.g. `CLEARSLOPE_GOOGLE_SHEET_ID`. Prefix is stored in the JSON config. Factory functions (`writer_from_env(prefix=...)`, `notifier_from_env(prefix=...)`) accept and scope on this prefix.

**Runtime state** (mutable): stored on `app.state`, `app.state.clients` (dict), `app.state.minute_counters` (dict), `app.state.pending_calls` (dict). Not thread-safe but safe for a single-process asyncio event loop.

**Mandatory env vars** are checked at startup with warnings logged (not hard failures except for client configs), and at request-time via `/healthz` which returns 503 if required vars are absent.

## Security Patterns

**PII in logs:** Full phone numbers are scrubbed before any log line via `_redact_payload_for_log()`. Only last-4 digits survive. Addresses are truncated to 24 characters. This is enforced in `tools/llm_proxy/function_tools.py`.

**PII in persistence:** The `PIIRedactor` in `tools/redact_pii.py` runs a 3-pass pipeline (Presidio NER → Claude Haiku contextual pass → audit) before any transcript is written to SQLite or logged as `REDACTED_TRANSCRIPT`. Caller number stored only as last-4 in all storage surfaces.

**Transcript opt-out:** The `set_recording_preference` tool allows callers to opt out mid-call. The `recording_state` dict is shared between the tool handler and the end-of-call persist path. `retain=False` skips all persistence and logs an audit line. The design explicitly guards against coercing a missing `record` field to `False` (which would manufacture a withdrawal).

**HMAC-keyed filename hashing:** Call filename/recording SIDs are hashed with HMAC-SHA256 (`REDACTION_HMAC_KEY` env var, minimum 16 bytes) before storage. Same input → same hash (stable), but non-reversible without the key. See `PIIRedactor.hash_filename()`.

**Twilio webhook signature validation:** `X-Twilio-Signature` is validated on all `/twilio/*` endpoints. The escape hatch `ALLOW_UNSIGNED_TWILIO=1` exists for tests and is checked explicitly, absent this flag, missing `TWILIO_AUTH_TOKEN` is logged as a warning and unsigned requests will fail at validation time.

**Signed voicemail playback URLs:** HMAC-SHA256 signed URLs with expiry timestamps (`tools/llm_proxy/voicemail_urls.py`). Secret sourced from `VOICEMAIL_URL_SECRET`. Expired or tampered tokens return 403/410.

**Sentry PII:** `send_default_pii=False` is hardcoded in `_init_sentry()`, Sentry never receives PII even when configured.

**GDPR/PIPA compliance modes:** Per-client `gdpr_mode` and `pipa_mode` flags in `ClientConfig` inject different prompt prefixes. `gdpr_mode` and `pipa_mode` are mutually exclusive (enforced with `ValueError` in `load_client_config()`). GDPR mode defaults to `retain=False` unless the caller affirms; PIPA mode defaults to `retain=True` (implied consent under PIPA s.7).

**Secrets:** `.env` file only. `load_dotenv()` at startup. No secrets in client JSON configs. `SECRETS.md` exists at project root for reference.
