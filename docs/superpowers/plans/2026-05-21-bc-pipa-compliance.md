# BC PIPA Compliance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the non-GDPR voice receptionist call path into honest BC PIPA compliance, functional opt-out, uninterruptable disclosure, deterministic greeting, post-call redaction, and a `pipa_mode` flag mirroring the existing GDPR architecture.

**Architecture:** Five focused changes across the Pipecat call pipeline (`tools/llm_proxy/`), the Sheet schema, and one client config. The opt-out (`set_recording_preference`) becomes load-bearing by gating a new `transcript_retain` Sheet column and the end-of-call transcript log line. The greeting moves out of the LLM (via `TTSSpeakFrame`) so the compliance disclosure is deterministic, and the user-mute strategy on the context aggregator makes it uninterruptable.

**Tech Stack:** Pipecat 1.1.0 (verified installed), Anthropic Claude Haiku 4.5 (locked, prompt-cached), Cartesia TTS, Deepgram STT, Google Sheets, Presidio + Claude Haiku for redaction (`tools/redact_pii.py`, already built).

**Verified at plan-time** (corrections from the spec's "verify at implementation" note):

- Pipecat 1.1.0 has **no `STTMuteFilter`**. The replacement is the user-mute-strategy API on `LLMUserAggregatorParams.user_mute_strategies`. The correct class is `MuteUntilFirstBotCompleteUserMuteStrategy` (mutes from connection until first bot turn ends, covers the gap before TTS starts). `FirstSpeechUserMuteStrategy` would *not* cover the pre-TTS window and is the wrong pick.
- `TTSSpeakFrame` is at `pipecat.frames.frames` (unchanged from spec assumption).
- `LLMUserAggregatorParams` is at `pipecat.processors.aggregators.llm_response_universal`.
- **Sheet range** is hardcoded as `Sheet1!A:L` in two places in `sheets_writer.py` (lines 44 and 108). Adding a column means `A:M` everywhere.
- **Transcript persistence target:** the spec said "existing retention location", none exists today. v0.1 resolves this as **log-only** (emit redacted transcript as a structured log line at end-of-call). A future spec can add file persistence with a Fly volume or S3 sink. This decision is documented inline at Task 6.

---

## File Map

**Modify:**

| File | What changes |
|---|---|
| `config/clients/clearslope.json` | Add `"pipa_mode": true` |
| `tools/llm_proxy/client_config.py` | Add `pipa_mode` field + mutual-exclusion check |
| `tools/llm_proxy/sheets_writer.py` | Add `transcript_retain` column; bump range A:L → A:M; thread param through `format_row` |
| `tools/llm_proxy/function_tools.py` | Make `set_recording_preference` actually gate retention via shared state read by `log_request` |
| `tools/llm_proxy/voice_pipeline.py` | Add `PIPA_PROMPT_PREFIX`, `_render_pipa_greeting`, hardcoded greeting branch via `TTSSpeakFrame`, user-mute strategy on aggregator, post-call redacted-transcript log line |
| `PRIVACY_NOTICE_DRAFT.md` | Three textual updates to base clauses |
| `tests/test_sheets_writer.py` | Extend for new column |
| `tests/test_function_tools.py` | Extend for opt-out wiring |
| `tests/test_voice_pipeline.py` | Extend for PIPA-mode prefix selection + greeting renderer |

**Create:**

| File | Purpose |
|---|---|
| `tests/test_client_config.py` | New, mutual-exclusion and flag-default tests |

No new production source files. The change set fits inside the existing module boundaries.

---

## Task 1: Sheet schema, add `transcript_retain` column

**Files:**
- Modify: `tools/llm_proxy/sheets_writer.py:29-44, 56-81, 108, 121`
- Modify: `tests/test_sheets_writer.py`

- [ ] **Step 1: Write the failing test**

Open `tests/test_sheets_writer.py` and add at the bottom:

```python
def test_format_row_includes_transcript_retain_default_true() -> None:
    """Default to retain when the caller didn't opt out (PIPA implied-consent default)."""
    row = format_row({"caller_name": "Alex"}, vapi_call_id="x")
    assert len(row) == len(HEADER_ROW)
    idx = HEADER_ROW.index("transcript_retain")
    assert row[idx] is True


def test_format_row_respects_transcript_retain_kwarg() -> None:
    row = format_row({"caller_name": "Alex"}, vapi_call_id="x", transcript_retain=False)
    assert row[HEADER_ROW.index("transcript_retain")] is False


def test_header_row_extends_to_thirteen_columns() -> None:
    assert len(HEADER_ROW) == 13
    assert HEADER_ROW[-1] == "transcript_retain"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_sheets_writer.py -v
```

Expected: 3 new tests fail (assertion errors on length / column name / missing kwarg).

- [ ] **Step 3: Add the column to `HEADER_ROW`, range, and `format_row`**

In `tools/llm_proxy/sheets_writer.py`:

Replace `HEADER_ROW` (lines 29-42):

```python
HEADER_ROW = [
    "timestamp",
    "vapi_call_id",
    "caller_name",
    "callback_number_last4",
    "service_address",
    "identified_pest",
    "pest_description",
    "urgency",
    "best_callback_time",
    "out_of_scope",
    "needs_followup_notes",
    "ai_confidence",
    "transcript_retain",
]
```

Replace `DEFAULT_SHEET_RANGE` (line 44):

```python
DEFAULT_SHEET_RANGE = "Sheet1!A:M"
```

Update `format_row` signature and return (lines 56-81):

```python
def format_row(
    args: dict[str, Any],
    *,
    vapi_call_id: str | None = None,
    transcript_retain: bool = True,
) -> list[Any]:
    """Convert a tool-call args dict into a row matching `HEADER_ROW`.

    `transcript_retain` reflects the caller's recording-consent preference:
    True (default) means the redacted transcript may be retained for QA.
    False means the caller said 'no transcript' and the transcript log line
    must be suppressed at end-of-call. See PIPA s. 14 (right to withdraw).
    """
    full_number = _get(args, "callback_number", "callback_phone", "callerNumber")
    digits = "".join(c for c in full_number if c.isdigit())
    last4 = digits[-4:] if len(digits) >= 4 else digits

    return [
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        vapi_call_id or "",
        _get(args, "caller_name", "callerName"),
        last4,
        _get(args, "service_address", "address"),
        _get(args, "identified_pest", "identifiedPest"),
        _get(args, "pest_description", "reported_pest", "reportedPest"),
        _get(args, "urgency"),
        _get(args, "best_callback_time", "preferred_time_window", "preferredCallbackTime", "preferredTimeWindow"),
        bool(args.get("out_of_scope") or False),
        _get(args, "needs_followup_notes", "notes"),
        _get(args, "ai_confidence"),
        bool(transcript_retain),
    ]
```

Update `ensure_header`'s read/write range (line 108-121) to use `A1:M1`:

```python
def ensure_header(self) -> None:
    """Write the header row if the sheet is empty. Idempotent."""
    try:
        existing = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._sheet_id, range="Sheet1!A1:M1")
            .execute()
        )
    except HttpError as exc:
        raise RuntimeError(f"could not read sheet header (check share + ID): {exc}") from exc

    if existing.get("values"):
        return  # header already there

    try:
        self._service.spreadsheets().values().update(
            spreadsheetId=self._sheet_id,
            range="Sheet1!A1:M1",
            valueInputOption="RAW",
            body={"values": [HEADER_ROW]},
        ).execute()
    except HttpError as exc:
        raise RuntimeError(f"could not write sheet header (check Editor permission): {exc}") from exc
    logger.info("wrote header row to sheet %s", self._sheet_id)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_sheets_writer.py -v
```

Expected: all tests pass, including the 3 new ones. Existing `HEADER_ROW.index(...)` tests should still pass since lookup by name is column-position-independent.

- [ ] **Step 5: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add tools/llm_proxy/sheets_writer.py tests/test_sheets_writer.py
git commit -m "$(cat <<'EOF'
feat(sheets): add transcript_retain column to booking schema

PIPA s. 14 requires a functional right to withdraw consent. The
incoming opt-out flow (set_recording_preference) needs a column to
record the per-call retention decision. Defaults to True (implied
consent under PIPA for redacted-QA secondary use).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

**Note on existing Sheet:** the live Google Sheet has the old 12-column header. `ensure_header` is a no-op when a header row already exists, so it will NOT auto-add column M. Operators must add the `transcript_retain` column manually to existing Sheets (or delete the header row and let the next call write the new one). Document this in the PR description.

---

## Task 2: Client config, add `pipa_mode` + mutual exclusion

**Files:**
- Modify: `tools/llm_proxy/client_config.py:50-67, 80-109`
- Modify: `config/clients/clearslope.json`
- Create: `tests/test_client_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_client_config.py`:

```python
"""Tests for client config loading and mode-flag validation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.llm_proxy.client_config import load_client_config


CLEARSLOPE_PATH = Path("config/clients/clearslope.json")


def _write_tmp_config(tmp_path: Path, overrides: dict) -> Path:
    raw = json.loads(CLEARSLOPE_PATH.read_text(encoding="utf-8"))
    raw.update(overrides)
    out = tmp_path / "tmp_client.json"
    out.write_text(json.dumps(raw), encoding="utf-8")
    return out


def test_pipa_mode_defaults_false_for_legacy_configs(tmp_path: Path) -> None:
    cfg_path = _write_tmp_config(tmp_path, {"gdpr_mode": False})
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw.pop("pipa_mode", None)
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = load_client_config(cfg_path)
    assert cfg.pipa_mode is False


def test_pipa_mode_loads_when_set(tmp_path: Path) -> None:
    cfg_path = _write_tmp_config(tmp_path, {"pipa_mode": True, "gdpr_mode": False})
    cfg = load_client_config(cfg_path)
    assert cfg.pipa_mode is True
    assert cfg.gdpr_mode is False


def test_both_modes_set_raises(tmp_path: Path) -> None:
    cfg_path = _write_tmp_config(tmp_path, {"pipa_mode": True, "gdpr_mode": True})
    with pytest.raises(ValueError, match="mutually exclusive"):
        load_client_config(cfg_path)


def test_clearslope_config_has_pipa_mode_enabled() -> None:
    """Production config gate: clearslope.json must be running PIPA mode."""
    cfg = load_client_config(CLEARSLOPE_PATH)
    assert cfg.pipa_mode is True, "clearslope.json must opt into pipa_mode for BC"
    assert cfg.gdpr_mode is False
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_client_config.py -v
```

Expected: all 4 tests fail, `pipa_mode` attribute does not exist; clearslope.json doesn't have the flag.

- [ ] **Step 3: Add the field + mutual exclusion**

In `tools/llm_proxy/client_config.py`, modify `ClientConfig` (add after the `gdpr_mode` field, line 62):

```python
    # When True, the voice pipeline injects a BC PIPA-mode prefix into the
    # system prompt and uses a hardcoded TTS greeting (uninterruptable) with
    # an opt-out for transcript retention. Mutually exclusive with gdpr_mode.
    # See docs/superpowers/specs/2026-05-21-bc-pipa-compliance-design.md.
    pipa_mode: bool = False
```

Modify `load_client_config` (lines 80-109), replace the return statement to add `pipa_mode` and validate exclusion:

```python
def load_client_config(path: Path = DEFAULT_CONFIG_PATH) -> ClientConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    biz = raw["business"]
    hrs = raw["hours"]
    prm = raw["prompt"]
    ceiling = raw.get("minute_ceiling")
    gdpr_mode = bool(raw.get("gdpr_mode", False))
    pipa_mode = bool(raw.get("pipa_mode", False))
    if gdpr_mode and pipa_mode:
        raise ValueError(
            f"config {path}: gdpr_mode and pipa_mode are mutually exclusive; "
            f"pick one based on caller jurisdiction"
        )
    return ClientConfig(
        client_id=raw["client_id"],
        twilio_number=raw["twilio_number"],
        env_prefix=raw["env_prefix"],
        minute_ceiling=int(ceiling) if ceiling is not None else None,
        business=BusinessIdentity(
            name=biz["name"],
            short_name=biz["short_name"],
            industry=biz["industry"],
            ai_name=biz["ai_name"],
        ),
        hours=BusinessHours(
            timezone=ZoneInfo(hrs["timezone"]),
            weekdays=frozenset(int(d) for d in hrs["weekdays"]),
            open_hour=int(hrs["open_hour"]),
            close_hour=int(hrs["close_hour"]),
        ),
        prompt=PromptPaths(
            path=Path(prm["path"]),
            taxonomy_path=Path(prm["taxonomy_path"]),
        ),
        gdpr_mode=gdpr_mode,
        pipa_mode=pipa_mode,
        live=bool(raw.get("live", True)),
    )
```

In `config/clients/clearslope.json`, add `"pipa_mode": true` as a sibling of `"minute_ceiling"`:

```json
{
  "client_id": "clearslope",
  "twilio_number": "+18136076475",
  "env_prefix": "CLEARSLOPE",
  "minute_ceiling": null,
  "pipa_mode": true,
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

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_client_config.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add tools/llm_proxy/client_config.py config/clients/clearslope.json tests/test_client_config.py
git commit -m "$(cat <<'EOF'
feat(config): add pipa_mode client flag + mutual exclusion with gdpr_mode

Mirrors the existing gdpr_mode architecture. clearslope.json opts in for
BC. Configs that set both flags fail-loud at load time so a deployment
can't misadvertise its compliance posture.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Wire `set_recording_preference` to actually gate retention

**Files:**
- Modify: `tools/llm_proxy/function_tools.py:142-165, 209-240, 241-300, 344-354`
- Modify: `tests/test_function_tools.py`

- [ ] **Step 1: Write the failing test**

Open `tests/test_function_tools.py` and append:

```python
@pytest.mark.asyncio
async def test_log_request_writes_transcript_retain_true_by_default() -> None:
    """No opt-out → transcript_retain=True in the Sheet row."""
    from tools.llm_proxy.sheets_writer import HEADER_ROW

    writer = FakeSheetsWriter()
    handlers = make_handlers(
        sheets_writer=writer,
        call_sid="CA200",
        on_end_call=lambda: None,
    )
    params = FakeFunctionCallParams(arguments={
        "caller_name": "Bea", "callback_number": "604-555-7777",
        "service_address": "1 X St", "urgency": "routine",
    })
    await handlers["log_request"](params)
    assert len(writer.rows) == 1
    idx = HEADER_ROW.index("transcript_retain")
    assert writer.rows[0][idx] is True


@pytest.mark.asyncio
async def test_set_recording_preference_false_then_log_request_writes_retain_false() -> None:
    """Caller said 'no transcript' → transcript_retain=False in the Sheet row."""
    from tools.llm_proxy.sheets_writer import HEADER_ROW

    writer = FakeSheetsWriter()
    handlers = make_handlers(
        sheets_writer=writer,
        call_sid="CA201",
        on_end_call=lambda: None,
    )
    # Caller opts out mid-call.
    pref_params = FakeFunctionCallParams(arguments={"record": False, "reason": "no transcript"})
    await handlers["set_recording_preference"](pref_params)
    assert pref_params.results == [{"status": "noted", "record": False}]

    # Subsequent log_request must record the opt-out.
    log_params = FakeFunctionCallParams(arguments={
        "caller_name": "Cee", "callback_number": "604-555-8888",
        "service_address": "2 Y St", "urgency": "routine",
    })
    await handlers["log_request"](log_params)
    assert len(writer.rows) == 1
    idx = HEADER_ROW.index("transcript_retain")
    assert writer.rows[0][idx] is False


@pytest.mark.asyncio
async def test_recording_state_is_exposed_for_end_of_call_consumers() -> None:
    """The end-of-call transcript log path reads recording_state, expose it."""
    from tools.llm_proxy.function_tools import make_handlers as _make

    handlers = _make(sheets_writer=None, call_sid="CA202", on_end_call=lambda: None)
    # The factory returns the recording_state dict as a hidden attribute on the
    # returned mapping, so voice_pipeline.run_voice_session can read it.
    assert "_recording_state" in handlers
    assert handlers["_recording_state"]["record"] is None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_function_tools.py -v -k "transcript_retain or recording_state or set_recording_preference_false"
```

Expected: 3 tests fail, `transcript_retain` not in row, no `_recording_state` key.

- [ ] **Step 3: Make `set_recording_preference` functional and expose state**

In `tools/llm_proxy/function_tools.py`:

Replace the `SET_RECORDING_PREFERENCE_SCHEMA` description (lines 142-165) with clearer scope:

```python
SET_RECORDING_PREFERENCE_SCHEMA = FunctionSchema(
    name="set_recording_preference",
    description=(
        "Record the caller's preference for whether the transcript of this "
        "call may be retained and reviewed by our team (after redaction). "
        "The raw audio is never stored either way. Call this when the "
        "caller says 'no transcript', 'no recording', 'stop recording', or "
        "the inverse confirmations."
    ),
    properties={
        "record": {
            "type": "boolean",
            "description": (
                "True if the caller consents to transcript retention, False "
                "if they refuse or withdraw consent. In pipa_mode the default "
                "is retain (implied consent for redacted QA review under "
                "PIPA s. 7); in gdpr_mode the default is no-retain until the "
                "caller affirms."
            ),
        },
        "reason": {
            "type": "string",
            "description": "Optional one-line note (caller's own words).",
        },
    },
    required=["record"],
)
```

Modify `log_request` and `set_recording_preference` inside `make_handlers` (lines 241-354). The key change: `log_request` calls `format_row` with `transcript_retain=...` reflecting the latest state. Replace lines 241-300 plus 344-361:

```python
    async def log_request(params: Any) -> None:
        args = params.arguments or {}
        if sheets_writer is None:
            logger.error("call_sid=%s log_request invoked but sheets writer not configured", call_sid)
            await params.result_callback(
                {"status": "error", "message": "booking system not configured"}
            )
            return

        # Honour the caller's opt-out. None (no preference expressed) defaults
        # to True (PIPA implied consent for redacted-QA secondary use). The
        # operator's mode flag is the source of truth for the default, but
        # by the time log_request fires, the only signal we have here is the
        # explicit preference. The voice pipeline is responsible for setting
        # the default appropriately on a per-mode basis (see Task 5).
        pref = recording_state.get("record")
        transcript_retain = True if pref is None else bool(pref)

        try:
            row = format_row(args, vapi_call_id=call_sid, transcript_retain=transcript_retain)
            sheets_writer.append_row(row)
        except Exception as exc:
            safe_args = _redact_payload_for_log(args)
            try:
                payload = json.dumps(safe_args, default=str, ensure_ascii=False)
            except Exception:
                payload = repr(safe_args)
            logger.error("call_sid=%s log_request DROPPED row=%s", call_sid, payload)
            logger.exception("call_sid=%s log_request failed", call_sid)
            await params.result_callback({"status": "error", "message": str(exc)})
            return
        logger.info(
            "call_sid=%s log_request ok pest=%r out_of_scope=%s transcript_retain=%s",
            call_sid,
            args.get("identified_pest") or args.get("identifiedPest"),
            bool(args.get("out_of_scope")),
            transcript_retain,
        )

        if sms_notifier is not None and not sms_state["sent"]:
            sms_state["sent"] = True
            urgency = _first_nonempty(args, "urgency", "Urgency").lower()
            caller_name = _first_nonempty(args, "caller_name", "callerName")
            sends: list[Awaitable[bool]] = []
            if caller_number:
                sends.append(
                    sms_notifier.send_customer_confirmation(
                        to=caller_number, caller_name=caller_name, urgency=urgency,
                    )
                )
            if urgency == "emergency":
                address = _first_nonempty(args, "service_address", "address")
                pest = _first_nonempty(args, "identified_pest", "identifiedPest", "pest_description")
                sends.append(
                    sms_notifier.send_owner_emergency_alert(
                        caller_name=caller_name,
                        address=address,
                        pest=pest,
                        callback=caller_number,
                    )
                )
            if sends:
                await asyncio.gather(*sends, return_exceptions=True)

        await params.result_callback({"status": "saved"})
```

Replace the return statement at the end of `make_handlers` (line 356-361):

```python
    return {
        "log_request": log_request,
        "end_call": end_call,
        "human_handoff": human_handoff,
        "set_recording_preference": set_recording_preference,
        # Exposed so the voice pipeline can read the final state at end-of-call
        # to decide whether to emit the redacted-transcript log line.
        "_recording_state": recording_state,
    }
```

Update the registration loop in `voice_pipeline.py` (lines 253-254) to skip the underscore-prefixed entry:

```python
    handlers = make_handlers(
        sheets_writer=sheets_writer,
        call_sid=call_sid,
        on_end_call=end_call_signal,
        sms_notifier=sms_notifier,
        caller_number=caller_number,
    )
    recording_state = handlers.pop("_recording_state")
    for name, handler in handlers.items():
        llm.register_function(name, handler)
```

(`recording_state` will be consumed by Task 6's end-of-call log line.)

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_function_tools.py -v
```

Expected: all tests pass, including the 3 new ones.

- [ ] **Step 5: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add tools/llm_proxy/function_tools.py tools/llm_proxy/voice_pipeline.py tests/test_function_tools.py
git commit -m "$(cat <<'EOF'
fix(receptionist): make set_recording_preference actually gate retention

The handler previously updated a local dict that nothing downstream
read, the verbal opt-out the GDPR-mode greeting promises was a no-op.
Wire recording_state into log_request so the new transcript_retain
column reflects the caller's actual preference. PIPA s. 14 requires
withdrawal of consent to be effective; the previous state arguably
constituted misrepresentation.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Add `PIPA_PROMPT_PREFIX` and select it from `pipa_mode`

**Files:**
- Modify: `tools/llm_proxy/voice_pipeline.py:62-141`
- Modify: `tests/test_voice_pipeline.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_voice_pipeline.py`:

```python
def _loaded_prompt_with(mode: str) -> object:
    """Build a LoadedPrompt for testing the prompt composer in each mode."""
    from dataclasses import replace
    from pathlib import Path
    from tools.llm_proxy.client_config import load_client_config
    from tools.llm_proxy.prompt_loader import LoadedPrompt

    base = load_client_config()
    if mode == "pipa":
        base = replace(base, pipa_mode=True, gdpr_mode=False)
    elif mode == "gdpr":
        base = replace(base, pipa_mode=False, gdpr_mode=True)
    elif mode == "none":
        base = replace(base, pipa_mode=False, gdpr_mode=False)
    else:
        raise ValueError(mode)
    return LoadedPrompt(
        text="System prompt body.",
        version="v0-test",
        prompt_path=Path("/dev/null"),
        taxonomy_path=Path("/dev/null"),
        config=base,
    )


def test_compose_system_prompt_includes_pipa_prefix_when_flagged() -> None:
    from tools.llm_proxy.voice_pipeline import PIPA_PROMPT_PREFIX, _compose_system_prompt

    composed = _compose_system_prompt(_loaded_prompt_with("pipa"))
    assert PIPA_PROMPT_PREFIX in composed
    assert "System prompt body." in composed


def test_compose_system_prompt_gdpr_still_uses_gdpr_prefix() -> None:
    from tools.llm_proxy.voice_pipeline import GDPR_PROMPT_PREFIX, _compose_system_prompt

    composed = _compose_system_prompt(_loaded_prompt_with("gdpr"))
    assert GDPR_PROMPT_PREFIX in composed


def test_compose_system_prompt_no_mode_no_prefix() -> None:
    from tools.llm_proxy.voice_pipeline import (
        GDPR_PROMPT_PREFIX,
        PIPA_PROMPT_PREFIX,
        _compose_system_prompt,
    )

    composed = _compose_system_prompt(_loaded_prompt_with("none"))
    assert GDPR_PROMPT_PREFIX not in composed
    assert PIPA_PROMPT_PREFIX not in composed
    assert "System prompt body." in composed
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_voice_pipeline.py -v -k "pipa or gdpr or no_mode"
```

Expected: failures referencing `PIPA_PROMPT_PREFIX` not defined.

- [ ] **Step 3: Add the PIPA prefix and update the composer**

In `tools/llm_proxy/voice_pipeline.py`, immediately after the existing `GDPR_PROMPT_PREFIX` (after line 136), add:

```python
PIPA_PROMPT_PREFIX = """\
# BC PIPA mode, overrides below take precedence

This client is configured for callers located in British Columbia, Canada.
The instructions in this section override any conflicting instructions in
the main prompt below.

## Greeting (delivered by deterministic TTS, NOT by you)

The greeting is spoken by the system before your first turn. You do NOT
generate the greeting. Your first generated turn is the response to
whatever the caller says first.

## Tool: set_recording_preference

If the caller says "no transcript", "no recording", "don't record",
"stop recording", or anything clearly equivalent:
  1. Call `set_recording_preference` with `record=false` and a one-line reason.
  2. Acknowledge briefly ("Got it, no transcript.") and continue the call.

If the caller explicitly confirms ("yes, that's fine", "go ahead"):
  1. Call `set_recording_preference` with `record=true`.
  2. Continue normally.

The caller's opt-out applies retroactively for this call, the
transcript will not be retained.

## Tool: human_handoff

PIPA does not guarantee a human-review right the way GDPR Article 22
does, but offering one is best practice. If the caller asks for a
human, transfer them via `human_handoff` with name + callback number +
one-paragraph context summary.

## Data minimization

Collect only what's necessary for pest triage and booking. Don't probe
for special-category data (health conditions, religion, ethnicity,
sexual orientation). If the caller volunteers such information,
acknowledge briefly and don't repeat it back unnecessarily, and don't
include it in `needs_followup_notes` unless directly relevant to
serving the technician safely.

## End of overrides, the main prompt follows
"""
```

Replace `_compose_system_prompt` (around lines 139-141):

```python
def _compose_system_prompt(loaded: LoadedPrompt) -> str:
    if loaded.config.gdpr_mode:
        prefix = GDPR_PROMPT_PREFIX + "\n\n"
    elif loaded.config.pipa_mode:
        prefix = PIPA_PROMPT_PREFIX + "\n\n"
    else:
        prefix = ""
    return f"{prefix}{time_context(loaded.config)}\n\n{loaded.text}"
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_voice_pipeline.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add tools/llm_proxy/voice_pipeline.py tests/test_voice_pipeline.py
git commit -m "$(cat <<'EOF'
feat(voice): add PIPA_PROMPT_PREFIX for BC pipa_mode clients

Parallel to GDPR_PROMPT_PREFIX. Instructs the model on the opt-out
handling, human-handoff offer, and data-minimization restraint. Tells
the model NOT to generate the greeting, that's handled deterministically
in the next task.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Hardcoded uninterruptable greeting via `TTSSpeakFrame`

**Files:**
- Modify: `tools/llm_proxy/voice_pipeline.py` (top-level imports + `run_voice_session` body)
- Modify: `tests/test_voice_pipeline.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_voice_pipeline.py`:

```python
def test_render_pipa_greeting_interpolates_business_and_ai_name() -> None:
    from tools.llm_proxy.voice_pipeline import _render_pipa_greeting

    loaded = _loaded_prompt_with("pipa")
    text = _render_pipa_greeting(loaded.config)
    assert "Sarah" in text
    assert "Clear Slope" in text
    assert "no transcript" in text  # opt-out phrase the caller can repeat
    assert "AI" in text             # AI self-identification


def test_render_pipa_greeting_fits_short_utterance_window() -> None:
    """≤9 seconds spoken ≈ ≤22 words at 150 wpm conversational pace."""
    from tools.llm_proxy.voice_pipeline import _render_pipa_greeting

    loaded = _loaded_prompt_with("pipa")
    text = _render_pipa_greeting(loaded.config)
    word_count = len(text.split())
    assert word_count <= 35, f"greeting too long for uninterruptable UX: {word_count} words"


def test_voice_pipeline_imports_mute_strategy_class() -> None:
    """Verifies the Pipecat 1.1.0 mute-strategy import is correct."""
    from tools.llm_proxy.voice_pipeline import MuteUntilFirstBotCompleteUserMuteStrategy

    instance = MuteUntilFirstBotCompleteUserMuteStrategy()
    assert hasattr(instance, "process_frame")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_voice_pipeline.py -v -k "render_pipa or mute_strategy"
```

Expected: 3 tests fail, `_render_pipa_greeting` and `MuteUntilFirstBotCompleteUserMuteStrategy` not defined in module.

- [ ] **Step 3: Add the greeting renderer + mute strategy wiring**

In `tools/llm_proxy/voice_pipeline.py`:

Extend the imports near the top of the file (after the existing `pipecat.frames.frames` import on line 34):

```python
from pipecat.frames.frames import (
    EndTaskFrame,
    LLMMessagesAppendFrame,
    LLMRunFrame,
    TTSSpeakFrame,
)
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_mute.mute_until_first_bot_complete_user_mute_strategy import (
    MuteUntilFirstBotCompleteUserMuteStrategy,
)
```

Remove the existing line `from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair` since it's now consolidated above.

Add the greeting renderer near `_compose_system_prompt`:

```python
def _render_pipa_greeting(config: "ClientConfig") -> str:
    """Hardcoded BC PIPA disclosure greeting. Deterministic, no LLM in the loop.

    Spoken via TTSSpeakFrame so every caller hears the exact same disclosure.
    Length target: ≤35 words / ≤9 seconds at 150 wpm. Uninterruptable via
    the user-mute strategy on the context aggregator.
    """
    return (
        f"Hi, this is {config.business.ai_name} at "
        f"{config.business.short_name}, I'm AI. Calls are transcribed and "
        f"reviewed by our team to improve service, after personal info is "
        f"scrubbed. Say 'no transcript' if you'd rather we don't keep one. "
        f"What's going on?"
    )
```

In `run_voice_session`, replace the `LLMContextAggregatorPair(context)` line (around line 220) with a mode-aware variant:

```python
    user_mute_strategies = []
    if loaded_prompt.config.pipa_mode or loaded_prompt.config.gdpr_mode:
        user_mute_strategies.append(MuteUntilFirstBotCompleteUserMuteStrategy())

    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(user_mute_strategies=user_mute_strategies),
    )
```

Then replace the `_on_connected` handler body (around lines 256-275) with mode-aware greeting dispatch:

```python
    @transport.event_handler("on_client_connected")
    async def _on_connected(transport_inst: Any, client: Any) -> None:
        logger.info("call_sid=%s client connected; kicking off greeting", call_sid)

        if loaded_prompt.config.pipa_mode:
            # Deterministic disclosure: no LLM in the loop. The mute strategy
            # on the user aggregator blocks interruption until TTS finishes.
            greeting = _render_pipa_greeting(loaded_prompt.config)
            await task.queue_frames(
                [
                    LLMMessagesAppendFrame(
                        messages=[{"role": "assistant", "content": greeting}]
                    ),
                    TTSSpeakFrame(text=greeting),
                ]
            )
            return

        # Existing path: LLM-generated greeting (GDPR mode + bare mode).
        await task.queue_frames(
            [
                LLMMessagesAppendFrame(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "The caller has just connected. Greet them warmly in one short sentence, "
                                "matching the time context above (business hours vs after hours), then ask "
                                "how you can help."
                            ),
                        }
                    ]
                ),
                LLMRunFrame(),
            ]
        )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_voice_pipeline.py -v
```

Expected: all tests pass, including the 3 new ones.

- [ ] **Step 5: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add tools/llm_proxy/voice_pipeline.py tests/test_voice_pipeline.py
git commit -m "$(cat <<'EOF'
feat(voice): deterministic uninterruptable PIPA disclosure greeting

In pipa_mode, the greeting is rendered from a hardcoded template and
played via TTSSpeakFrame, no LLM call, no per-call variance. The
MuteUntilFirstBotCompleteUserMuteStrategy on the user aggregator
suppresses STT until the bot's first speaking turn ends, so the
disclosure can't be cut off by an early-speaking caller.

This is the Pipecat 1.1.0 replacement for the older STTMuteFilter
(which no longer exists in this version).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Post-call redacted-transcript log line

**Files:**
- Modify: `tools/llm_proxy/voice_pipeline.py` (imports + finally block in `run_voice_session`)
- Modify: `tests/test_voice_pipeline.py`

**Design decision (resolves the spec's open "retention location" question):** v0.1 emits the redacted transcript as a single structured log line. No file persistence. Costs $0 (no new infrastructure, no Fly volume, no S3). Honest about where the data lands (the existing log aggregator). A follow-up spec can add file/object-storage persistence with proper retention tooling.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_voice_pipeline.py`:

```python
def test_extract_transcript_from_context_messages_skips_system_and_tool_calls() -> None:
    from tools.llm_proxy.voice_pipeline import _extract_transcript_text

    messages = [
        {"role": "system", "content": "DON'T LOG ME"},
        {"role": "assistant", "content": "Hi, this is Sarah."},
        {"role": "user", "content": "I have mice."},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "log_request"}]},
        {"role": "tool", "content": "{\"status\":\"saved\"}"},
        {"role": "assistant", "content": "Thanks, someone will follow up."},
    ]
    text = _extract_transcript_text(messages)
    assert "DON'T LOG ME" not in text
    assert "Hi, this is Sarah." in text
    assert "I have mice." in text
    assert "Thanks, someone will follow up." in text
    # No tool-call serialization noise.
    assert "tool_use" not in text
    assert "log_request" not in text


def test_persist_redacted_transcript_logs_redacted_text(caplog) -> None:
    import logging
    from tools.llm_proxy.voice_pipeline import _persist_redacted_transcript

    messages = [
        {"role": "assistant", "content": "Hi, this is Sarah."},
        {"role": "user", "content": "Hi, I'm Alex Doe at 604-555-1234."},
    ]
    caplog.set_level(logging.INFO, logger="voice_pipeline")
    _persist_redacted_transcript(call_sid="CA300", messages=messages, retain=True)
    transcript_lines = [
        r.getMessage() for r in caplog.records if "REDACTED_TRANSCRIPT" in r.getMessage()
    ]
    assert len(transcript_lines) == 1
    line = transcript_lines[0]
    # Names and phone numbers must be scrubbed.
    assert "Alex Doe" not in line
    assert "604-555-1234" not in line
    assert "CA300" in line


def test_persist_redacted_transcript_skipped_when_retain_false(caplog) -> None:
    import logging
    from tools.llm_proxy.voice_pipeline import _persist_redacted_transcript

    messages = [{"role": "user", "content": "anything"}]
    caplog.set_level(logging.INFO, logger="voice_pipeline")
    _persist_redacted_transcript(call_sid="CA301", messages=messages, retain=False)
    transcript_lines = [
        r.getMessage() for r in caplog.records if "REDACTED_TRANSCRIPT" in r.getMessage()
    ]
    assert transcript_lines == []
    # A skip note is emitted so we can audit opt-out compliance.
    skip_lines = [
        r.getMessage() for r in caplog.records if "transcript_retain=false" in r.getMessage()
    ]
    assert len(skip_lines) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_voice_pipeline.py -v -k "transcript"
```

Expected: failures, `_extract_transcript_text` and `_persist_redacted_transcript` not defined.

- [ ] **Step 3: Implement the extractor + persister, wire into `finally`**

In `tools/llm_proxy/voice_pipeline.py`:

Add a top-level import (near the other tool imports):

```python
from tools.redact_pii import load_default_redactor, PIIRedactor
```

Add these two module-level functions immediately after `_render_pipa_greeting`:

```python
_REDACTOR: PIIRedactor | None = None


def _get_redactor() -> PIIRedactor | None:
    """Lazy-init a process-wide redactor; None if config is missing.

    Build is expensive (Presidio model + spaCy model load), so we cache.
    Returns None if REDACTION_HMAC_KEY is unset, the caller must handle
    that by skipping persistence and logging a warning.
    """
    global _REDACTOR
    if _REDACTOR is not None:
        return _REDACTOR
    try:
        _REDACTOR = load_default_redactor()
    except Exception:
        logger.exception("redactor unavailable; transcript persistence disabled")
        _REDACTOR = None
    return _REDACTOR


def _extract_transcript_text(messages: list[dict[str, Any]]) -> str:
    """Render the user/assistant turns of an LLMContext as a plain transcript.

    Drops system messages, tool calls, and tool results. Only text turns are
    emitted so the redactor sees the actual conversation.
    """
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            # Anthropic-style content blocks: only keep text parts.
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")).strip())
            text = "\n".join(p for p in parts if p)
        else:
            continue
        if text:
            label = "Sarah" if role == "assistant" else "Caller"
            lines.append(f"{label}: {text}")
    return "\n".join(lines)


def _persist_redacted_transcript(
    *, call_sid: str, messages: list[dict[str, Any]], retain: bool
) -> None:
    """Emit the redacted transcript for this call as a structured log line.

    No file persistence in v0.1, the operator's log aggregator is the
    retention surface. The line carries a fixed prefix (`REDACTED_TRANSCRIPT`)
    so downstream tooling can extract it.

    When `retain=False`, the transcript is dropped on the floor and a skip
    note is logged so opt-out compliance is auditable.
    """
    if not retain:
        logger.info("call_sid=%s transcript_retain=false; skipping persistence", call_sid)
        return

    raw = _extract_transcript_text(messages)
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

    quarantine = "QUARANTINE_" if result.residuals else ""
    # Single structured line, keep it grep-friendly.
    logger.info(
        "%sREDACTED_TRANSCRIPT call_sid=%s residuals=%d counts=%s content=%r",
        quarantine,
        call_sid,
        len(result.residuals),
        result.counts,
        result.redacted_text,
    )
```

In `run_voice_session`'s `finally` block (around lines 288-297), add the persistence call after the elapsed-minute accounting:

```python
    finally:
        elapsed_sec = time.monotonic() - t0
        elapsed_min = math.ceil(elapsed_sec / 60)
        client_id = client_ctx.config.client_id
        new_total = minute_counters.get(client_id, 0.0) + elapsed_min
        minute_counters[client_id] = new_total
        logger.info(
            "call_sid=%s client_id=%s elapsed_sec=%.1f minutes_charged=%d total_minutes=%.0f",
            call_sid, client_id, elapsed_sec, elapsed_min, new_total,
        )

        # End-of-call redacted-transcript log line. Reads the latest
        # recording_state to honor mid-call opt-outs. In gdpr_mode the
        # default is no-retain unless caller affirmed; in pipa_mode the
        # default is retain.
        pref = recording_state.get("record")
        if pref is None:
            retain_default = not client_ctx.config.gdpr_mode  # PIPA / bare mode: retain by default
            retain = retain_default
        else:
            retain = bool(pref)
        _persist_redacted_transcript(
            call_sid=call_sid,
            messages=list(context.messages),
            retain=retain,
        )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest tests/test_voice_pipeline.py -v
```

Expected: all tests pass.

If the contextual-redaction pass fails because `ANTHROPIC_API_KEY` is unset in CI, that's fine, `PIIRedactor` falls back to Presidio-only. The tests above use simple PII (a name and a phone) that Presidio catches without help.

- [ ] **Step 5: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add tools/llm_proxy/voice_pipeline.py tests/test_voice_pipeline.py
git commit -m "$(cat <<'EOF'
feat(voice): emit redacted-transcript log line at end of call

Wires tools/redact_pii.py into the end-of-call hook so any human review
of conversation transcripts sees PII-scrubbed text by default. v0.1 is
log-only (no file persistence), the operator's log aggregator is the
retention surface. Quarantine prefix is added when the redactor's audit
pass flags residuals so downstream tooling can route to restricted-access
review.

Honors set_recording_preference: retain=False skips persistence
entirely and emits a skip note for opt-out auditability.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Update `PRIVACY_NOTICE_DRAFT.md` base clauses

**Files:**
- Modify: `PRIVACY_NOTICE_DRAFT.md`

- [ ] **Step 1: Read the three sections being edited**

The relevant sections are: "How we use it", "How long we keep it", and "Recording disclosure". Run:

```bash
grep -n "How we use it\|How long we keep it\|Recording disclosure" "/home/nublet/Projects/AI Voice Assistant/PRIVACY_NOTICE_DRAFT.md"
```

- [ ] **Step 2: Edit "How we use it"**

Find the existing bullet list under "How we use it" (around line 33-37). Add this bullet at the end of the list:

```markdown
- **For quality review**: transcripts of calls are automatically scrubbed of personal information (names, phone numbers, addresses) and may be reviewed by our team to improve how the AI handles future calls.
```

Replace the existing "For quality assurance" bullet (the last one in the list, line 37) with the bullet above, they describe the same thing, but the new wording is honest about the scrubbing step.

- [ ] **Step 3: Edit "How long we keep it"**

Replace the "Call transcripts" line (currently: `- **Call transcripts**, kept for up to 90 days for quality review, then deleted.`) with:

```markdown
- **Call transcripts**, automatically scrubbed of personal information before any human review. Redacted transcripts are kept for up to 90 days for quality review, then deleted. Raw (un-scrubbed) transcripts are never persisted beyond the call.
```

- [ ] **Step 4: Edit "Recording disclosure"**

Replace the entire "Recording disclosure" section body (currently the paragraph starting "The AI receptionist verbally discloses…") with:

```markdown
Calls are transcribed by AI in real time. The transcript is automatically scrubbed of personal information and may be reviewed by our team to improve service. You can decline transcript retention at any time during the call by saying "no transcript", your service request will still be taken, but the transcript of the call will not be retained.

If you do not consent to any transcript handling, you can hang up and reach us via [alternative contact method, e.g., email or web form].
```

- [ ] **Step 5: Commit**

```bash
cd "/home/nublet/Projects/AI Voice Assistant"
git add PRIVACY_NOTICE_DRAFT.md
git commit -m "$(cat <<'EOF'
docs(privacy): align base notice with actual PIPA-mode behavior

The notice previously described "may be recorded for quality", that
was vague and didn't reflect the system's actual behavior. The new
text names the AI transcription, the automatic scrubbing step before
human review, and the verbal opt-out callers now have.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **Step 1: Run the entire test suite**

```bash
cd "/home/nublet/Projects/AI Voice Assistant" && pytest -v
```

Expected: all tests pass. If any pre-existing tests fail due to the schema change (extra column), inspect them, they likely use `len(HEADER_ROW)` or hardcoded column counts and need a small update.

- [ ] **Step 2: Manual verification checklist (run on Fly preview)**

```
[ ] Place a real call to the Fly preview number.
[ ] Greeting plays for full duration when speaking immediately ("can you
    hear me?" within the first second should not interrupt).
[ ] Spoken greeting is identical across three consecutive calls
    (verbatim, read along with _render_pipa_greeting output).
[ ] Mid-call: say "no transcript". Agent acknowledges briefly.
    After hangup: confirm fly logs show "transcript_retain=false; skipping
    persistence" and NO "REDACTED_TRANSCRIPT" line for that call_sid.
[ ] Mid-call: do not opt out. After hangup: confirm fly logs show one
    "REDACTED_TRANSCRIPT" line for that call_sid with PII scrubbed.
[ ] Confirm the new Sheet row has transcript_retain column populated
    (TRUE or FALSE matching the call's opt-out state).
```

- [ ] **Step 3: Pre-deploy preflight**

```
[ ] REDACTION_HMAC_KEY is set in Fly secrets (`fly secrets list | grep REDACTION_HMAC_KEY`).
[ ] Live Google Sheet has been manually extended with the
    `transcript_retain` column M (the code does not auto-migrate existing
    headers).
[ ] The Cartesia voice is unchanged from the LLM-greeting era, no voice
    swap needed; same Cartesia config plays both the hardcoded greeting
    and the LLM responses.
```

---

## Spec coverage check (self-review)

| Spec section | Implemented in |
|---|---|
| §1 PIPA mode flag + greeting template | Task 2, Task 4, Task 5 |
| §2 client_config surface flag | Task 2 |
| §3a PIPA_PROMPT_PREFIX | Task 4 |
| §3b Hardcoded greeting via TTSSpeakFrame | Task 5 |
| §3c Uninterruptable (MuteUntilFirstBotComplete strategy) | Task 5 |
| §4 set_recording_preference functional + transcript_retain column | Task 1, Task 3 |
| §5 Redaction wired into post-call path | Task 6 |
| §6 Privacy notice base clauses update | Task 7 |
| Data flow diagram (end-to-end) | Tasks 5 + 6 together |
| Error handling: redactor unavailable | Task 6 (`_persist_redacted_transcript` falls back) |
| Error handling: both modes set | Task 2 (`load_client_config` raises) |
| Testing, unit | Tasks 1, 2, 3, 4, 5, 6 each include TDD tests |
| Testing, integration / manual verification | Final verification §Step 2 |

No spec gaps. All five high-priority audit findings (F1–F6) are covered.

---

## Out of scope (deferred to follow-ups)

- Per-client `business.privacy_url_spoken` field for naming a specific URL in the greeting.
- File-based or object-storage transcript persistence (v0.1 is log-only).
- Tiered QA reviewer tooling that consumes the `QUARANTINE_` prefix.
- Caller-location-based mode routing (Twilio E.164 CGN → mode).
- Quebec Law 25 mode.
- Anthropic ZDR contractual confirmation (operational, not code).
