# Phase 1: Pilot Ready - Pattern Map

**Mapped:** 2026-05-07
**Files analyzed:** 7
**Analogs found:** 7 / 7

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `tools/llm_proxy/client_config.py` | model/config | transform | self (extend existing) | exact |
| `tools/llm_proxy/main.py` | provider/startup | request-response | self (extend existing) | exact |
| `tools/llm_proxy/voice_routes.py` | controller | request-response | self (extend existing) | exact |
| `tools/llm_proxy/sheets_writer.py` | service | CRUD | self (extend existing) | exact |
| `tools/llm_proxy/voice_pipeline.py` | service | streaming | self (extend existing) | exact |
| `config/clients/clearslope.json` | config | - | self (canonical schema) | exact |
| `config/clients/allgreen.json` | config | - | `config/clients/clearslope.json` | exact |

---

## Pattern Assignments

### `tools/llm_proxy/client_config.py` (model, transform)

**Change:** Add `twilio_number: str` and `minute_ceiling: int | None` fields to `ClientConfig`. Add a `scan_client_configs(dir)` function that loads all `*.json` files in `config/clients/`.

**Existing dataclass pattern** (lines 44–48):
```python
@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    business: BusinessIdentity
    hours: BusinessHours
    prompt: PromptPaths
```
Add two fields after `prompt`:
```python
    twilio_number: str # E.164, e.g. "+17781234567", routing key
    minute_ceiling: int | None  # None = no ceiling (Clear Slope); int for capped clients
```

**Existing loader pattern** (lines 51–74):
```python
def load_client_config(path: Path = DEFAULT_CONFIG_PATH) -> ClientConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    ...
    return ClientConfig(
        client_id=raw["client_id"],
        ...
        prompt=PromptPaths(...),
    )
```
New scanner follows the same `json.loads(path.read_text(...))` + field extraction idiom:
```python
def scan_client_configs(config_dir: Path) -> dict[str, ClientConfig]:
    """Return dict[twilio_number, ClientConfig] for all *.json in config_dir."""
    result: dict[str, ClientConfig] = {}
    for path in sorted(config_dir.glob("*.json")):
        cfg = load_client_config(path)
        result[cfg.twilio_number] = cfg
    return result
```

**JSON field to add** (mirrors `clearslope.json` key names):
```json
"twilio_number": "+1XXXXXXXXXX",
"minute_ceiling": 200
```
`null` in JSON → `None` in Python (Clear Slope gets `"minute_ceiling": null` or omits the field; loader must use `raw.get("minute_ceiling")` not `raw["minute_ceiling"]`).

---

### `tools/llm_proxy/main.py` (provider/startup, request-response)

**Change:** Replace single-client `app.state.sheets_writer / sms_notifier / loaded_prompt` with `app.state.clients: dict[twilio_number, ClientContext]` built at startup. `ClientContext` is a small dataclass grouping the per-client objects.

**Existing `app.state` stash pattern** (lines 194–196):
```python
app.state.loaded_prompt = loaded
app.state.sheets_writer = sheets_writer
app.state.sms_notifier = sms_notifier
```
Replace with:
```python
app.state.clients = clients   # dict[twilio_number, ClientContext]
```

**Existing factory call pattern** (lines 156–179):
```python
sheets_writer = writer_from_env()
if sheets_writer is None:
    logger.warning("Google Sheets writer not configured ...")
else:
    logger.info("Google Sheets writer ready (sheet=%s)", ...)

sms_notifier = notifier_from_env(
    business_name=loaded.config.business.name,
    business_short_name=loaded.config.business.short_name,
)
```
Phase 1 loops this pattern per client JSON, using `{PREFIX}_GOOGLE_SHEET_ID`, `{PREFIX}_TWILIO_SMS_FROM`, `{PREFIX}_OWNER_NOTIFY_NUMBER` env vars (prefix comes from `client_config.env_prefix`).

**Existing startup import block** (lines 40–46):
```python
from .prompt_loader import load_prompt
from .sheets_writer import writer_from_env
from .sms_notifier import notifier_from_env
from .voice_pipeline import RECEPTIONIST_MODEL
from .voice_routes import router as voice_router
```
Add:
```python
from .client_config import scan_client_configs, ClientConfig
from pathlib import Path
```

**Per-client minute counter pattern** (new, inline in `_build_app`):
```python
# In-memory minute counters. dict[client_id, float], reset on restart (D-08).
minute_counters: dict[str, float] = {}
app.state.minute_counters = minute_counters
```

---

### `tools/llm_proxy/voice_routes.py` (controller, request-response)

**Change:** (1) `/twilio/voice` extracts `To`, looks up client, checks ceiling. (2) `/twilio/stream` pulls client context from the per-call map and passes it to `run_voice_session`.

**Existing `app.state` read pattern** (lines 149–152):
```python
state = websocket.app.state
loaded_prompt = state.loaded_prompt
sheets_writer = state.sheets_writer
sms_notifier = getattr(state, "sms_notifier", None)
```
Phase 1 replaces with:
```python
state = websocket.app.state
client_ctx = state.clients.get(to_number)  # looked up from per-call map by call_sid
```

**Existing `_validate_twilio_signature` pattern** (lines 61–79):
```python
def _validate_twilio_signature(request: Request, form: dict[str, str]) -> None:
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    ...
    validator = RequestValidator(auth_token)
    if not validator.validate(url, form, signature):
        raise HTTPException(status_code=403, detail="invalid twilio signature")
```
Phase 1 makes `auth_token` a parameter (sourced from `client_ctx.auth_token` or global env), signature stays identical otherwise.

**Existing `twilio_voice` handler pattern** (lines 82–106):
```python
@router.post("/twilio/voice")
async def twilio_voice(request: Request) -> Response:
    form = dict(await request.form())
    _validate_twilio_signature(request, form)
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")
    ...
    return Response(content=str(response), media_type="application/xml")
```
Phase 1 adds:
```python
    to_number = form.get("To", "")
    client_ctx = request.app.state.clients.get(to_number)
    if client_ctx is None:
        logger.error("twilio_voice: unknown To number %s", to_number)
        raise HTTPException(status_code=404, detail="unknown destination number")
    # Ceiling check (D-09)
    if client_ctx.config.minute_ceiling is not None:
        used = request.app.state.minute_counters.get(client_ctx.config.client_id, 0.0)
        if used >= client_ctx.config.minute_ceiling:
            # TTS rejection via TwiML <Say> then <Hangup>
            ...
```

**Existing WebSocket state pull + `run_voice_session` call** (lines 149–168):
```python
await run_voice_session(
    websocket=websocket,
    stream_sid=stream_sid,
    call_sid=call_sid,
    loaded_prompt=loaded_prompt,
    sheets_writer=sheets_writer,
    sms_notifier=sms_notifier,
    caller_number=caller_number,
)
```
Phase 1 adds `client_ctx=client_ctx` parameter and uses a short-lived `dict[call_sid, to_number]` on `app.state` (set in `/twilio/voice`, consumed and deleted in `/twilio/stream`).

**TwiML ceiling-rejection pattern** (new, no existing analog, use Twilio VoiceResponse directly):
```python
from twilio.twiml.voice_response import VoiceResponse
resp = VoiceResponse()
resp.say("Our answering service is temporarily unavailable, please call back during business hours.")
resp.hangup()
return Response(content=str(resp), media_type="application/xml")
```

---

### `tools/llm_proxy/sheets_writer.py` (service, CRUD)

**Change:** Follow the `TODO(multi-client)` comment exactly. `writer_from_env()` becomes `writer_from_env(prefix: str)`, reads `{PREFIX}_GOOGLE_SHEET_ID` instead of bare `GOOGLE_SHEET_ID`. Called once per client at startup.

**Existing `writer_from_env` pattern** (lines 237–254):
```python
def writer_from_env() -> SheetsWriter | None:
    # TODO(multi-client): GOOGLE_SHEET_ID becomes per-tenant when we onboard
    # client #2. Replace with a `dict[twilio_to_number, SheetsWriter]` keyed
    # by the inbound Twilio number, constructed at app startup.
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if not sa or not sheet_id:
        return None
    try:
        writer = SheetsWriter(service_account_file=sa, sheet_id=sheet_id)
        writer.ensure_header()
        return writer
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        logger.warning("SheetsWriter unavailable: %s", exc)
        return None
```
Phase 1 signature change:
```python
def writer_from_env(prefix: str) -> SheetsWriter | None:
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()  # still global
    sheet_id = os.environ.get(f"{prefix}_GOOGLE_SHEET_ID", "").strip()
    ...
```
`SheetsWriter.__init__` is unchanged. The dict-of-writers is assembled in `main.py`, not here.

---

### `tools/llm_proxy/voice_pipeline.py` (service, streaming)

**Change:** `run_voice_session()` receives `client_ctx` (the new `ClientContext` dataclass). It uses `client_ctx.loaded_prompt`, `client_ctx.sheets_writer`, `client_ctx.sms_notifier` instead of the current positional args. It also records wall-clock duration and updates `minute_counters` after the pipeline exits.

**Existing `run_voice_session` signature** (lines 65–74):
```python
async def run_voice_session(
    *,
    websocket: WebSocket,
    stream_sid: str,
    call_sid: str,
    loaded_prompt: LoadedPrompt,
    sheets_writer: SheetsWriter | None,
    sms_notifier: SmsNotifier | None = None,
    caller_number: str = "",
) -> None:
```
Phase 1 target signature:
```python
async def run_voice_session(
    *,
    websocket: WebSocket,
    stream_sid: str,
    call_sid: str,
    client_ctx: "ClientContext", # new, replaces loaded_prompt/sheets_writer/sms_notifier
    caller_number: str = "",
    minute_counters: dict[str, float],  # shared mutable ref from app.state
) -> None:
```

**Existing env-read pattern at function top** (lines 82–87):
```python
twilio_account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
deepgram_api_key = os.environ.get("DEEPGRAM_API_KEY", "")
cartesia_api_key = os.environ.get("CARTESIA_API_KEY", "")
cartesia_voice_id = os.environ.get("CARTESIA_VOICE_ID", "").strip()
```
These remain global env reads (shared keys). `twilio_auth_token` will be read from `client_ctx.auth_token` instead of `os.environ` once available via the registry.

**Existing `runner.run(task)` try/finally pattern** (lines 202–208):
```python
runner = PipelineRunner(handle_sigint=False)
try:
    await runner.run(task)
except Exception:
    logger.exception("call_sid=%s pipeline error", call_sid)
finally:
    logger.info("call_sid=%s session finished", call_sid)
```
Phase 1 adds minute accounting in the `finally` block:
```python
import math, time
_t0 = time.monotonic()
...
finally:
    elapsed_sec = time.monotonic() - _t0
    elapsed_min = math.ceil(elapsed_sec / 60)
    client_id = client_ctx.config.client_id
    minute_counters[client_id] = minute_counters.get(client_id, 0.0) + elapsed_min
    logger.info(
        "call_sid=%s session finished elapsed_sec=%.1f minutes_charged=%d total_minutes=%.0f client=%s",
        call_sid, elapsed_sec, elapsed_min, minute_counters[client_id], client_id,
    )
```

---

### `config/clients/clearslope.json` (config, canonical schema)

**Change:** Add `twilio_number` and `minute_ceiling` fields to match the extended `ClientConfig` dataclass. `minute_ceiling` is `null` for Clear Slope (no cap, D-07).

**Current schema** (lines 1–19, entire file):
```json
{
  "client_id": "clearslope",
  "business": {
    "name": "Clear Slope Pest Control",
    "short_name": "Clear Slope",
    "industry": "pest control and wildlife exclusion",
    "ai_name": "Sarah"
  },
  "hours": {
    "timezone": "America/Vancouver",
    "weekdays": [0, 1, 2, 3, 4],
    "open_hour": 9,
    "close_hour": 17
  },
  "prompt": {
    "path": "tools/prompts/receptionist_v0.3_data_driven.md",
    "taxonomy_path": "tools/prompts/pest_taxonomy.json"
  }
}
```
Add after `"prompt"` block:
```json
  "twilio_number": "+1XXXXXXXXXX",
  "minute_ceiling": null
```

---

### `config/clients/allgreen.json` (config, NEW)

**Analog:** `config/clients/clearslope.json`, copy structure exactly, replace values.

**Target schema:**
```json
{
  "client_id": "allgreen",
  "business": {
    "name": "All Green Pest Control",
    "short_name": "All Green",
    "industry": "pest control",
    "ai_name": "Sarah"
  },
  "hours": {
    "timezone": "America/Vancouver",
    "weekdays": [0, 1, 2, 3, 4],
    "open_hour": 9,
    "close_hour": 17
  },
  "prompt": {
    "path": "tools/prompts/receptionist_v0.3_data_driven.md",
    "taxonomy_path": "tools/prompts/pest_taxonomy.json"
  },
  "twilio_number": "+1XXXXXXXXXX",
  "minute_ceiling": 200
}
```
`ai_name`, `hours`, `prompt` paths are placeholders, planner must flag these for human confirmation. `minute_ceiling: 200` is locked per D-07 / CONTEXT specifics.

---

## Shared Patterns

### Per-client factory pattern
**Source:** `tools/llm_proxy/sms_notifier.py` lines 136–166 (`notifier_from_env`) and `tools/llm_proxy/sheets_writer.py` lines 237–254 (`writer_from_env`)
**Apply to:** All `*_from_env` functions refactored for multi-client

Both follow identical structure:
```python
def factory_from_env(prefix: str, ...) -> Thing | None:
    required_var = os.environ.get(f"{prefix}_VAR_NAME", "").strip()
    if not required_var:
        return None
    try:
        obj = Thing(...)
        return obj
    except (SomeError,) as exc:
        logger.warning("Thing unavailable: %s", exc)
        return None
```

### `app.state` resource stash
**Source:** `tools/llm_proxy/main.py` lines 194–196
**Apply to:** All new per-client state stored at startup

```python
app.state.clients = clients          # dict[twilio_number, ClientContext]
app.state.minute_counters = {}       # dict[client_id, float]
app.state.pending_calls = {} # dict[call_sid, twilio_number], ephemeral bridge
```

### Structured JSON logging
**Source:** `tools/llm_proxy/voice_pipeline.py` lines 202–208
**Apply to:** Minute ceiling log entry

Follow the `key=value` flat format already used:
```python
logger.info(
    "call_sid=%s session finished elapsed_sec=%.1f minutes_charged=%d total_minutes=%.0f client=%s",
    call_sid, elapsed_sec, elapsed_min, minute_counters[client_id], client_id,
)
```

### Graceful `None`-guard at startup
**Source:** `tools/llm_proxy/main.py` lines 156–163
**Apply to:** Every per-client factory call in the startup loop

```python
writer = writer_from_env(prefix=cfg.env_prefix)
if writer is None:
    logger.warning("client %s: SheetsWriter not configured, bookings will fail", cfg.client_id)
else:
    logger.info("client %s: SheetsWriter ready", cfg.client_id)
```

### Twilio signature validation
**Source:** `tools/llm_proxy/voice_routes.py` lines 61–79
**Apply to:** `_validate_twilio_signature`, make `auth_token` a parameter

```python
def _validate_twilio_signature(request: Request, form: dict[str, str], auth_token: str) -> None:
    if not auth_token:
        if os.environ.get("ALLOW_UNSIGNED_TWILIO", "").strip().lower() in {"1", "true", "yes"}:
            logger.warning("...")
            return
        raise HTTPException(status_code=500, detail="server misconfigured")
    ...
    validator = RequestValidator(auth_token)
    if not validator.validate(url, form, signature):
        raise HTTPException(status_code=403, detail="invalid twilio signature")
```

---

## No Analog Found

| File | Role | Data Flow | Reason |
|---|---|---|---|
| `ClientContext` dataclass (new, in `main.py` or `client_config.py`) | model | - | No existing per-call context bundle; must be created |

`ClientContext` groups `config: ClientConfig`, `loaded_prompt: LoadedPrompt`, `sheets_writer: SheetsWriter | None`, `sms_notifier: SmsNotifier | None`, `auth_token: str`. Planner defines this as a simple `@dataclass` following the frozen dataclass pattern from `client_config.py` lines 21–48 (but `frozen=False` or regular class is fine since it holds mutable-ish service objects).

---

## Metadata

**Analog search scope:** `tools/llm_proxy/`, `config/clients/`
**Files scanned:** 7 source files read in full
**Pattern extraction date:** 2026-05-07
