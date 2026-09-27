# Testing Patterns

**Analysis Date:** 2026-05-25

## Test Framework

**Runner:** pytest `>=8.3` (declared in `requirements.txt`)

**Async support:** pytest-asyncio `>=0.24`. Async tests use `@pytest.mark.asyncio` decorator explicitly, no `asyncio_mode = "auto"` config found, so the decorator is required on every async test function.

**No pyproject.toml or pytest.ini** at the project root, no enforced coverage thresholds, no custom markers registered, no filter-warnings config.

**Run Commands:**
```bash
pytest tests/                          # Run all tests
pytest tests/test_redact_pii.py -v     # Single file, verbose
pytest tests/ -k "voicemail"           # Filter by name
pytest tests/ --co -q                  # Collect/list tests without running
```

## Test File Organization

**Location:** All tests live in `tests/` at the project root (not co-located with source).

**Naming convention:** `test_<module_name>.py` mirrors the source module name:
- `tests/test_redact_pii.py` → `tools/redact_pii.py`
- `tests/test_proxy_app.py` → `tools/llm_proxy/main.py`
- `tests/test_voice_pipeline.py` → `tools/llm_proxy/voice_pipeline.py`
- `tests/test_function_tools.py` → `tools/llm_proxy/function_tools.py`
- `tests/test_transcript_store.py` → `tools/llm_proxy/transcript_store.py`
- `tests/test_voicemail.py` → `tools/llm_proxy/voicemail.py`
- `tests/test_voicemail_urls.py` → `tools/llm_proxy/voicemail_urls.py`
- `tests/test_voicemail_retention.py` → `tools/voicemail_retention.py`
- `tests/test_sms_notifier.py` → `tools/llm_proxy/sms_notifier.py`
- `tests/test_sheets_writer.py` → `tools/llm_proxy/sheets_writer.py`
- `tests/test_digest.py` → `tools/llm_proxy/digest.py`
- `tests/test_digest_cli.py` → `tools/llm_proxy/digest_cli.py`
- `tests/test_client_config.py` → `tools/llm_proxy/client_config.py`
- `tests/test_prompt_loader.py` → `tools/llm_proxy/prompt_loader.py`
- `tests/test_multitenant_routing.py` → multi-module routing integration

**Test data:** `tests/replay_scenarios/*.json`, three JSON scenario files (`honey_bees_out_of_scope.json`, `mice_renter_apartment.json`, `wasp_emergency_allergy.json`) containing caller-turn scripts. These appear to be for manual LLM eval, not automated tests, no runner discovered in the test suite that loads them.

## Test Infrastructure

**conftest.py** (`tests/conftest.py`): minimal, only adds the project root to `sys.path` so `from tools.xxx import ...` works without installing the package.

**No shared fixtures** beyond `tmp_path` (pytest builtin). Each test file defines its own fake/stub classes inline.

**FastAPI test client:** `fastapi.testclient.TestClient` used in `test_proxy_app.py`, `test_multitenant_routing.py`, `test_voicemail.py`. The app is built fresh per test via a `@pytest.fixture()` calling `main_module._build_app()` with `monkeypatch` to suppress real credentials.

**SQLite tests** use `tmp_path` to give each test a fresh database path, no shared state between tests in `test_transcript_store.py`.

**Module reload pattern** for the proxy app tests: since `_build_app()` is a factory function (not a module-level singleton), tests can call it repeatedly with different env var states via `monkeypatch`.

## Test Structure

**Suite organization:**
```python
# Typical synchronous test
def test_<what_it_does>() -> None:
    # Arrange
    redactor = PIIRedactor(hmac_key=..., use_contextual_pass=False)
    # Act
    result = redactor.redact_text("Call me at 604-555-0123")
    # Assert
    assert "[PHONE]" in result.redacted_text
    assert result.is_clean

# Async test
@pytest.mark.asyncio
async def test_<async_thing>() -> None:
    handlers = make_handlers(sheets_writer=writer, ...)
    params = FakeFunctionCallParams(arguments={...})
    await handlers["log_request"](params)
    assert params.results == [{"status": "saved"}]
```

**Fixture scope:** `scope="module"` used only in `test_redact_pii.py` for the `PIIRedactor` fixture (expensive Presidio init). All other fixtures use the default `scope="function"`.

**Inline fake classes** are the dominant pattern, small, purpose-built fakes defined in each test file:
- `FakeSheetsWriter` (in `test_function_tools.py`, `test_voicemail.py`)
- `FakeSmsNotifier` (in `test_function_tools.py`)
- `FakeFunctionCallParams` (in `test_function_tools.py`)
- `FakeTwilioClient` / `_FakeRecordingsResource` (in `test_voicemail_retention.py`)
- `_FakeService` / `_FakeValues` (in `test_sheets_writer.py` for Google API chain)

## Mocking

**Framework:** `unittest.mock.patch` via `patch.dict(os.environ, ...)` for environment variables in some tests; `pytest.MonkeyPatch` (`monkeypatch` fixture) is the primary patching mechanism everywhere else.

**Monkeypatch patterns:**
```python
# Env vars
monkeypatch.setenv("ALLOW_UNSIGNED_TWILIO", "1")
monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

# Module attributes
monkeypatch.setattr(voice_pipeline, "_REDACTOR", _FakeRedactor())
monkeypatch.setattr(voice_pipeline, "_REDACTOR_INITIALIZED", True)
monkeypatch.setattr(transcript_store, "save", _fake_save)

# Async HTTP (httpx)
monkeypatch.setattr("httpx.AsyncClient.get", _fake_get)
```

**What is mocked:**
- All external network calls: Google Sheets API, Twilio REST API, httpx HTTP client
- Expensive model loads: `PIIRedactor` (replaced with `_FakeRedactor`) when testing the pipeline boundary
- `transcript_store.save` to assert the wiring without an actual SQLite write
- `os.environ` for credentials that would be absent in CI

**What is NOT mocked:**
- The Presidio NER engine, `test_redact_pii.py` instantiates a real `PIIRedactor(use_contextual_pass=False)`, so spaCy and Presidio must be installed
- SQLite, `test_transcript_store.py` uses a real SQLite database at `tmp_path`
- `config/clients/*.json` files, several tests load the real `clearslope.json` config directly
- The Pipecat import (guarded with `pytest.importorskip("pipecat", ...)` in `test_voice_pipeline.py` so it skips gracefully when not installed)

## Fixtures and Factories

**Test data factories** in `test_digest.py`:
```python
def _row(*, ts: str, name: str = "", pest: str = "...", urgency: str = "routine", ...) -> CallRow:
    return CallRow(timestamp=_utc(ts), ...)
```

**Config mutation** via `dataclasses.replace()`:
```python
config = replace(load_client_config(), gdpr_mode=False, pipa_mode=True)
```

**Fake writer construction** bypassing `__init__` (no creds needed):
```python
writer = SheetsWriter.__new__(SheetsWriter)
writer._sheet_id = "SHEET_ID_TEST"
writer._service = _FakeService(get_result)
```

**Fixture location:** All in-file, no shared fixtures in `conftest.py` beyond the sys.path patch.

## Coverage

**Requirements:** None enforced (no `--cov` config, no coverage threshold).

**Modules with test coverage:**

| Module | Test File | Coverage depth |
|--------|-----------|---------------|
| `tools/redact_pii.py` | `test_redact_pii.py` | Deep, all three passes, edge cases, spelled-out phones |
| `tools/llm_proxy/function_tools.py` | `test_function_tools.py` | Deep, all tools, error paths, SMS idempotency, PIPA opt-out |
| `tools/llm_proxy/voice_pipeline.py` | `test_voice_pipeline.py` | Medium, prompt composition, transcript persistence, opt-out; pipeline instantiation skipped |
| `tools/llm_proxy/main.py` | `test_proxy_app.py` | Medium, /healthz, /twilio/voice, auth, signature rejection |
| `tools/llm_proxy/sms_notifier.py` | `test_sms_notifier.py` | Deep, all urgency levels, anonymous callers, Twilio failures |
| `tools/llm_proxy/transcript_store.py` | `test_transcript_store.py` | Deep, round-trip, idempotency, quarantine, schema creation |
| `tools/llm_proxy/voicemail.py` | `test_voicemail.py` | Deep, TwiML, callbacks, playback proxy, concurrent voicemails |
| `tools/llm_proxy/voicemail_urls.py` | `test_voicemail_urls.py` | Deep, sign/verify, expiry, tamper detection |
| `tools/llm_proxy/sheets_writer.py` | `test_sheets_writer.py` | Medium, `format_row`, voicemail tab, transcript update |
| `tools/llm_proxy/digest.py` | `test_digest.py` | Deep, all digest fields, edge cases, rendering |
| `tools/llm_proxy/digest_cli.py` | `test_digest_cli.py` | Medium, run(), dry-run, empty sheet |
| `tools/llm_proxy/client_config.py` | `test_client_config.py` | Medium, mode flags, mutual exclusion, production gate |
| `tools/llm_proxy/prompt_loader.py` | `test_prompt_loader.py` | Light, smoke, determinism, interpolation |
| `tools/voicemail_retention.py` | `test_voicemail_retention.py` | Deep, prune logic, dry-run, 404 handling, argparse default |
| Multitenant routing | `test_multitenant_routing.py` | Medium, routing, ceiling, minute accounting |

## Test Quality Assessment

**Tests are behavior-focused, not implementation-focused.** Most tests assert on observable outcomes (HTTP status codes, row contents, log lines, SMS body text) rather than implementation details like call counts.

**Regression anchors are explicit.** Tests carry comments like "Receptionist call path must be Haiku, never Opus or Sonnet", "Locks in the contracts established by Plans 01-03", and "PIPA s. 14: a withdrawal must be the caller's act". These tie tests to specific compliance and design decisions.

**Production gate tests** exist: `test_clearslope_config_has_pipa_mode_enabled()` will fail if someone disables PIPA mode in the production config. `test_healthz_reports_locked_voice_model()` will fail if the model constant changes to non-Haiku. `test_audio_days_default_is_one_in_real_parser()` catches accidental reversion of the retention default.

**Async timing tests** use short but non-zero `asyncio.sleep()` waits (`0.05–0.15s`) to let background tasks settle. These are fragile under high CI load but acceptable for the voicemail timeout scenarios tested.

**One weak pattern:** `test_sheets_writer.py` bypasses `SheetsWriter.__init__` via `__new__` to inject a fake service. This works but means the real initialization path (OAuth setup) has no test coverage.

## Gaps

**Critical untested paths:**

1. **`tools/llm_proxy/voice_routes.py`**, no tests. The WebSocket upgrade, `pending_calls` registration, and `run_voice_session()` dispatch are only exercised via the `/twilio/voice` TwiML endpoint test (which doesn't actually open a WebSocket).

2. **`tools/llm_proxy/text_chat_handler.py`**, no dedicated test file. The `strip_tags()` function and `handle_text_chat()` have no unit tests. The demo widget's `/text-chat` endpoint has no test coverage at all.

3. **`tools/llm_proxy/logging_config.py`**, no tests for `JsonFormatter` or `setup_logging()`.

4. **`tools/llm_proxy/prompt_loader.py`**, only smoke tested. No test for what happens when the prompt file or taxonomy file is missing.

5. **`tools/retention_enforcer.py`**, no test file. The file-deletion logic is tested indirectly via `test_voicemail_retention.py` for a different module; `retention_enforcer.py` itself has no tests.

6. **`tests/replay_scenarios/*.json`**, three realistic caller scenarios exist with no runner. No automated test exercises the LLM end-to-end with these inputs.

7. **`tools/llm_proxy/sheets_writer.py` `append_row()` and `append_to_tab()`**, the real Google API write paths are not tested; only `format_row()` (pure) and `update_voicemail_transcript()` (via fake) are covered.

8. **`tools/data_subject_request.py`, `tools/ingest_transcripts.py`, `tools/extract_sales_reps.py`**, no tests for any of these CLI tools.

9. **Multi-tenant healthz**, `/healthz` behaviour when `clients_with_no_sheet` is non-empty and `live=True` is not tested directly.
