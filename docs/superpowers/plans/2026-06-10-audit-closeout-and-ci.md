# Audit Close-Out + CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every genuinely-open finding from `docs/audit-2026-06-01-security-performance.md`, verify SC1 is fixed, and add GitHub Actions CI.

**Architecture:** All changes are $0 code/config-only. Findings already shipped on this branch (SC1, S4, S6, S8, S9, S11, S12, S15-partial, S16-partial, S18, SC7, P4, S14-streaming) get their stale doc statuses corrected. Remaining code work: P2 (dead Smart Turn warm-up), P3 (warm PII redactor), P5 (prompt-cache stability), S13 (logout), S15 (chunked-body cap), SC6 (per-thread SQLite), S17 (multi-stage Dockerfile), S16 (hash lockfile), CI workflow. SC2 and the S14/S10 remainders are deliberately deferred with documented rationale.

**Tech Stack:** FastAPI/Starlette, Pipecat 1.2.1, pytest (253 passing baseline, ~40s), ruff 0.15.14, Docker, GitHub Actions. Always use `.venv/bin/python` (system python3 has older pipecat).

**Ground-truth verification already done (2026-06-10):** SC1's fix is live, `voice_routes.py:_resolve_stream_to_number` resolves the client from a signed `to`/`sig`/`exp` TwiML Stream parameter (machine-independent), with `pending_calls` kept only as a same-machine deploy-window fallback. Do not re-implement.

---

### Task 1: P2, delete the dead Smart Turn warm-up

`LocalSmartTurnAnalyzerV3` is constructed at startup (`main.py:78,82`) but `grep -n "smart_turn\|SmartTurn\|turn_analyzer" tools/llm_proxy/voice_pipeline.py` confirms no pipeline uses it. It burns ~4s of synchronous startup and pins an ONNX graph in RAM on a 1GB box. The VAD warm-up IS load-bearing (per-call `SileroVADAnalyzer()` at `voice_pipeline.py:463` relies on warm imports/page cache), keep it.

**Files:**
- Modify: `tools/llm_proxy/main.py:60-90` (`_warm_pipecat_models`)
- Test: `tests/test_proxy_app.py` (add)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_proxy_app.py`:

```python
def test_warmup_does_not_construct_smart_turn(monkeypatch):
    """P2: LocalSmartTurnAnalyzerV3 is unused by any pipeline, warming it
    burned ~4s of startup and pinned an ONNX graph in RAM for nothing."""
    import inspect
    from tools.llm_proxy import main as main_mod
    src = inspect.getsource(main_mod._warm_pipecat_models)
    assert "LocalSmartTurnAnalyzerV3" not in src
    assert "warmed_turn_analyzer" not in src
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `.venv/bin/python -m pytest tests/test_proxy_app.py::test_warmup_does_not_construct_smart_turn -q`
Expected: FAIL (source still contains `LocalSmartTurnAnalyzerV3`)

- [ ] **Step 3: Implement**

In `tools/llm_proxy/main.py`, edit `_warm_pipecat_models`: remove the `LocalSmartTurnAnalyzerV3` import, the `app.state.warmed_turn_analyzer = ...` line and its `t_turn` timing; drop the `app.state.warmed_vad` stash but keep constructing one throwaway `SileroVADAnalyzer()` (the construction is what loads the Silero ONNX weights into the page cache). Update the docstring (it currently claims holding instances cuts per-call cost, it doesn't; warm imports/page cache do). Resulting body:

```python
def _warm_pipecat_models(app: FastAPI) -> None:
    """Pre-load the Silero VAD model at app startup.

    Without this, the first inbound call after process restart paid ~10 s of
    cold-start cost (module imports + ONNX graph init on shared-cpu-1x),
    pushing Media Stream WS setup past Twilio's tolerance and producing the
    "3 beeps then disconnect" symptom (Twilio error 31921). See
    call_drops_diagnosis.txt at the project root for the evidence trail.

    Constructing one throwaway analyzer here loads the Silero weights into
    the OS page cache and primes module imports; per-call code still builds a
    fresh SileroVADAnalyzer (VAD audio-history buffers must not be shared
    between concurrent callers) but each construction is ~100 ms instead of
    ~5 s. The Smart Turn analyzer is NOT warmed, no pipeline uses it (P2).
    """
    import time as _time
    t0 = _time.perf_counter()
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    t_import = _time.perf_counter()
    SileroVADAnalyzer()
    t_vad = _time.perf_counter()
    logger.info(
        "pipecat_warm_done import_ms=%.0f vad_ms=%.0f total_ms=%.0f",
        (t_import - t0) * 1000,
        (t_vad - t_import) * 1000,
        (t_vad - t0) * 1000,
    )
```

First `grep -rn "warmed_vad\|warmed_turn_analyzer" tools/ tests/`, if any other reader exists (none found in prior survey), update it.

- [ ] **Step 4: Run the new test + full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 254 passed

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/main.py tests/test_proxy_app.py
git commit -m "perf(startup): drop dead Smart Turn warm-up (P2), ~4s startup + RAM saved"
```

---

### Task 2: P3, warm the PII redactor at startup

First call-end after every restart pays a multi-second spaCy `en_core_web_sm` load. Warm it where the VAD is warmed.

**Files:**
- Modify: `tools/llm_proxy/main.py` (`_warm_pipecat_models`, end of body)
- Test: `tests/test_proxy_app.py` (add)

- [ ] **Step 1: Write the failing test**

```python
def test_warmup_primes_pii_redactor(monkeypatch):
    """P3: the first post-restart call-end paid a multi-second spaCy model
    load in the hangup path; startup warm-up must prime it instead."""
    import inspect
    from tools.llm_proxy import main as main_mod
    src = inspect.getsource(main_mod._warm_pipecat_models)
    assert "_get_redactor" in src
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `.venv/bin/python -m pytest tests/test_proxy_app.py::test_warmup_primes_pii_redactor -q`

- [ ] **Step 3: Implement**

At the end of `_warm_pipecat_models` (after the VAD timing block), add:

```python
    # P3: prime the PII redactor (Presidio AnalyzerEngine + spaCy model) so the
    # first call-end after a restart doesn't pay the multi-second model load.
    # _get_redactor returns None when redaction isn't configured, harmless.
    from .voice_pipeline import _get_redactor
    t_red0 = _time.perf_counter()
    _get_redactor()
    logger.info("redactor_warm_done ms=%.0f", (_time.perf_counter() - t_red0) * 1000)
```

- [ ] **Step 4: Run full suite**, `.venv/bin/python -m pytest tests/ -q`, expect 255 passed. If suite runtime balloons because every app-build now loads spaCy, gate with `if os.environ.get("SKIP_REDACTOR_WARM") != "1"` and set that env in `tests/conftest.py`.

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/main.py tests/test_proxy_app.py tests/conftest.py
git commit -m "perf(startup): warm PII redactor at boot (P3), first hangup no longer loads spaCy"
```

---

### Task 3: P5, move volatile time_context out of the cached system prompt

The per-call time line at the FRONT of the ~8.5k-token system block forces a cache WRITE every call instead of a read (~$9–10/mo at 1k calls, plus first-turn latency). Fix both paths.

**Files:**
- Modify: `tools/llm_proxy/voice_pipeline.py:217-232` (`_compose_system_prompt`), `:502-506` (context construction)
- Modify: `tools/llm_proxy/text_chat_handler.py:50-55`
- Test: `tests/test_prompt_cache_stability.py` (create)

- [ ] **Step 1: Verify the pipecat adapter behavior (read-only)**

Run: `grep -n "system\|cache_control" .venv/lib/python*/site-packages/pipecat/adapters/services/anthropic_adapter.py | head -30`
Confirm: non-initial `system`-role messages are converted to user role, and `enable_prompt_caching` marks the initial system block. If the adapter instead puts the cache breakpoint somewhere that makes this fix a no-op, STOP and re-plan this task.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_prompt_cache_stability.py`:

```python
"""P5: the static system prompt must be byte-stable across calls so Anthropic
prompt caching gets cache READS, not a cache WRITE per call. The volatile
time line rides outside the cached span."""
from tools.llm_proxy.voice_pipeline import _compose_system_prompt
from tools.llm_proxy.prompt_loader import time_context


def _loaded(tmp_path, monkeypatch):
    # Build a minimal LoadedPrompt via the real loader against a stub prompt file.
    from tools.llm_proxy.prompt_loader import load_prompt
    from tools.llm_proxy.client_config import load_client_config
    import json, pathlib
    # Reuse an existing client config so business fields interpolate.
    cfg = load_client_config(pathlib.Path("config/clients/clearslope.json"))
    return load_prompt(cfg)


def test_system_prompt_is_time_stable(monkeypatch):
    loaded = _loaded(None, monkeypatch)
    a = _compose_system_prompt(loaded)
    # Freeze a different wall-clock for the second render.
    import tools.llm_proxy.voice_pipeline as vp
    b = _compose_system_prompt(loaded)
    assert a == b
    assert "[Current time:" not in a  # exact marker per prompt_loader.time_context


def test_time_context_still_reaches_the_model():
    loaded = _loaded(None, None)
    tc = time_context(loaded.config)
    assert tc # non-empty, it must be injected as a separate message
```

Adjust the `[Current time:` marker to whatever `time_context()` actually emits, check `tools/llm_proxy/prompt_loader.py:33-46` first and use a stable substring of it.

- [ ] **Step 3: Run them, expect the stability test to FAIL** (time line currently embedded)

Run: `.venv/bin/python -m pytest tests/test_prompt_cache_stability.py -q`

- [ ] **Step 4: Implement, voice path**

In `voice_pipeline.py`, `_compose_system_prompt` last line becomes:

```python
    return f"{prefix}{loaded.text}"
```

Update its docstring: time context is injected as a separate non-cached message (P5); the prefix is per-tenant-stable so it may stay in the cached block.

At the context construction (`voice_pipeline.py:502-506`):

```python
    system_prompt = _compose_system_prompt(loaded_prompt)
    context = LLMContext(
        messages=[
            {"role": "system", "content": system_prompt},
            # P5: volatile time line rides OUTSIDE the cached system block,             # pipecat's anthropic adapter converts non-initial system messages
            # to user role, so the big static block stays a stable cache prefix.
            {"role": "system", "content": time_context(loaded_prompt.config)},
        ],
        tools=TOOLS_SCHEMA,
    )
```

The LLM-greeting instruction at `voice_pipeline.py:580-587` says "matching the time context above", still true (the time message precedes it); no change needed.

- [ ] **Step 5: Implement, text-chat path**

In `text_chat_handler.py`:

```python
    response = await client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=DEFAULT_MAX_TOKENS,
        system=[
            # Static prompt is the cached prefix (P5)...
            {"type": "text", "text": loaded.text, "cache_control": {"type": "ephemeral"}},
            # ...volatile time line trails it, outside the cached span.
            {"type": "text", "text": time_context(loaded.config)},
        ],
        messages=[{"role": m.role, "content": m.content} for m in body.messages],
    )
```

(Anthropic caches the prefix up to and including the block marked `cache_control`; a trailing unmarked block doesn't invalidate it.) Check existing text-chat tests (`grep -rn "handle_text_chat\|full_prompt" tests/`) and update any that assert the old composition.

- [ ] **Step 6: Run full suite**, `.venv/bin/python -m pytest tests/ -q`. Fix any test asserting time-in-system-prompt.

- [ ] **Step 7: Commit**

```bash
git add tools/llm_proxy/voice_pipeline.py tools/llm_proxy/text_chat_handler.py tests/
git commit -m "perf(llm): keep system prompt cache-stable; inject time context as a separate message (P5)"
```

---

### Task 4: S13, /auth/logout (token revocation)

Expiry sweep already shipped; revocation is the missing half.

**Files:**
- Modify: `tools/llm_proxy/main.py` (next to `auth_demo`, ~line 486)
- Test: `tests/test_proxy_app.py` (add)

- [ ] **Step 1: Write the failing test**

Find how existing tests obtain a demo token (see `test_auth_demo_accepts_valid_credentials` in `tests/test_proxy_app.py`) and mirror that setup. Then:

```python
def test_auth_logout_revokes_token(client_with_demo_creds):
    # login
    r = client_with_demo_creds.post("/auth/demo", json={"username": "u", "password": "p"})
    token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    # logout
    r = client_with_demo_creds.post("/auth/logout", headers=headers)
    assert r.status_code == 200
    # token must now be rejected by /text-chat
    r = client_with_demo_creds.post(
        "/text-chat", headers=headers,
        json={"session_id": "s1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


def test_auth_logout_without_token_is_noop():
    # use the same fixture/client as above
    ...  # POST /auth/logout with no Authorization header -> 200, no error
```

Use the actual fixture/credential names found in the existing tests, do not invent `client_with_demo_creds` if a different fixture exists.

- [ ] **Step 2: Run, expect FAIL with 404** on `/auth/logout`.

- [ ] **Step 3: Implement** in `main.py` right after `auth_demo`:

```python
    @app.post("/auth/logout")
    async def auth_logout(authorization: str | None = Header(default=None)) -> Any:
        """Revoke a demo bearer token (S13). Idempotent: unknown/absent tokens
        return 200 so the widget can always clear local state."""
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:]
        _demo_tokens.pop(token, None)
        return JSONResponse({"ok": True})
```

- [ ] **Step 4: Run full suite**, expect all green.

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/main.py tests/test_proxy_app.py
git commit -m "feat(auth): add /auth/logout demo-token revocation (S13)"
```

---

### Task 5: S15 completion, cap chunked bodies with no Content-Length

The existing middleware only checks the `Content-Length` header; a chunked POST bypasses it and can OOM the 1GB box.

**Files:**
- Modify: `tools/llm_proxy/main.py` (new ASGI middleware + exception handler, near the S15 block ~line 268)
- Test: `tests/test_proxy_app.py` (add)

- [ ] **Step 1: Write the failing test**

```python
def test_chunked_body_without_content_length_is_capped(app_client_factory_or_existing_fixture):
    """S15: a streamed/chunked body (no Content-Length) must be rejected at the
    cap, not buffered until OOM."""
    # Build the app with a tiny cap so the test is fast.
    import os
    os.environ["MAX_REQUEST_BYTES"] = "1024"
    # ... rebuild app/client per the pattern used by other env-sensitive tests ...
    def body_gen():
        for _ in range(64):
            yield b"x" * 64  # 4 KiB total, no Content-Length (chunked)
    r = client.post("/auth/demo", content=body_gen())
    assert r.status_code == 413
```

Mirror the app-rebuild pattern other env-var tests in `test_proxy_app.py` use (the cap is read at `_build_app` time). `httpx`/Starlette `TestClient` sends a generator body as chunked.

- [ ] **Step 2: Run, expect FAIL** (request is buffered and processed, status 400/401, not 413).

- [ ] **Step 3: Implement** in `main.py`. Module level (near the other helpers):

```python
class BodyTooLarge(Exception):
    """Raised by the body-cap receive wrapper when a streamed body exceeds
    MAX_REQUEST_BYTES (S15). Converted to HTTP 413 by an exception handler,     body parsing always happens before the response starts, so the handler
    can still send a clean response."""


class _BodySizeLimitMiddleware:
    """Pure-ASGI middleware: counts streamed request-body bytes and raises
    BodyTooLarge past the cap. Complements (does not replace) the cheap
    Content-Length header check in the HTTP middleware."""

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge()
            return message

        await self.app(scope, limited_receive, send)
```

In `_build_app`, right after the Content-Length block (keep that, it rejects cheaply before any read):

```python
    app.add_middleware(_BodySizeLimitMiddleware, max_bytes=max_request_bytes)
    app.add_exception_handler(
        BodyTooLarge,
        lambda request, exc: StarletteJSONResponse(
            status_code=413, content={"error": "request body too large"}
        ),
    )
```

Note middleware ordering: `add_middleware` prepends, so register it AFTER the `@app.middleware("http")` logger if needed for the exception handler to catch, verify with the test; if the exception escapes as a 500, register the handler on the outermost app or convert inside `limited_receive` by sending the 413 directly. The test is the arbiter.

- [ ] **Step 4: Run full suite.** Also sanity-check the Twilio webhook path still works (existing voice-route tests cover it).

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/main.py tests/test_proxy_app.py
git commit -m "fix(security): cap chunked request bodies without Content-Length (S15)"
```

---

### Task 6: SC6, per-thread SQLite connections

The single shared connection + global `threading.Lock` serializes every read/write; after P1's thread offloading, simultaneous hang-ups queue behind it. WAL mode (already on) supports concurrent readers + one writer, use thread-local connections and let SQLite arbitrate.

**Files:**
- Modify: `tools/llm_proxy/transcript_store.py:78-95` (`_get_connection`, `_LOCK` usage throughout)
- Test: `tests/test_transcript_store.py` (add)

- [ ] **Step 1: Write the failing test**

```python
def test_connections_are_per_thread(tmp_path):
    """SC6: each thread gets its own connection so WAL-mode concurrency is
    usable; a single shared connection + global lock serialized every
    read/write across all calls."""
    import threading
    from tools.llm_proxy import transcript_store as ts
    db = str(tmp_path / "t.db")
    conns = []
    def grab():
        conns.append(id(ts._get_connection(db)))
    t1 = threading.Thread(target=grab); t2 = threading.Thread(target=grab)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert conns[0] != conns[1]


def test_concurrent_saves_do_not_corrupt(tmp_path):
    import threading
    from tools.llm_proxy import transcript_store as ts
    db = str(tmp_path / "t.db")
    errors = []
    def write(i):
        try:
            ts.save_minute_counter(client_id=f"c{i % 3}", total_minutes=float(i), db_path=db)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors
    assert len(ts.load_minute_counters(db_path=db)) == 3
```

Match the real function signatures in `transcript_store.py` (check `save_minute_counter`/`load_minute_counters` kwargs before writing).

- [ ] **Step 2: Run, expect the per-thread test to FAIL** (same connection id).

- [ ] **Step 3: Implement.** In `transcript_store.py`: replace the `_CONNECTIONS` dict cache with a `threading.local()` keyed by db path; keep `_LOCK` ONLY around first-time schema init (`executescript`) to avoid an init race; drop `with _LOCK:` around the individual `execute/commit` calls (WAL + `timeout=5.0` handles writer contention).

```python
_LOCAL = threading.local()
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[str] = set()


def _get_connection(db_path: str) -> sqlite3.Connection:
    """One connection per (thread, db_path), SC6. WAL mode allows concurrent
    readers + one writer; per-thread connections stop Python from serializing
    every read against every write behind one global lock."""
    cache = getattr(_LOCAL, "conns", None)
    if cache is None:
        cache = _LOCAL.conns = {}
    conn = cache.get(db_path)
    if conn is not None:
        return conn
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    with _SCHEMA_LOCK:
        if db_path not in _SCHEMA_READY:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
            _SCHEMA_READY.add(db_path)
    cache[db_path] = conn
    return conn
```

Note `check_same_thread=False` is no longer needed (connections never cross threads). Then remove the now-redundant `with _LOCK:` wrappers in `save`, `save_minute_counter`, `save_call_usage`, `purge_expired`, etc., and delete `_LOCK`/`_CONNECTIONS` if unused. Read the whole file first; keep any function-level behavior (best-effort error swallowing) identical.

- [ ] **Step 4: Run full suite**, transcript-store tests + retention + DSAR tests all touch this module; all must stay green.

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/transcript_store.py tests/test_transcript_store.py
git commit -m "perf(store): per-thread SQLite connections, drop global read/write lock (SC6)"
```

---

### Task 7: S17, multi-stage Dockerfile (no compiler in runtime)

**Files:**
- Modify: `infra/fly/Dockerfile`
- Test: `tests/test_container_hardening.py` (extend, read it first and follow its existing style; it statically inspects the Dockerfile)

- [ ] **Step 1: Write the failing test** (adapt names to the file's existing patterns):

```python
def test_runtime_stage_has_no_build_toolchain():
    """S17: build-essential must only exist in the builder stage."""
    text = DOCKERFILE.read_text()
    stages = text.split("FROM ")
    assert len(stages) >= 3  # base split artifact + builder + runtime
    runtime = stages[-1]
    assert "build-essential" not in runtime
```

- [ ] **Step 2: Run, expect FAIL** (single stage contains build-essential).

- [ ] **Step 3: Rewrite `infra/fly/Dockerfile`:**

```dockerfile
# --- builder: compile wheels with the full toolchain (S17) ---
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# --- runtime: no compiler, only runtime libs (S17) ---
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      libsndfile1 \
      sqlite3 \
      gosu \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged user to run the app (S9). The container still STARTS as root so
# entrypoint.sh can chown the Fly volume, then drops to this user via gosu.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app app

COPY requirements.txt ./
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels \
 && python -m spacy download en_core_web_sm

COPY tools ./tools
COPY config ./config
COPY infra/fly/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh \
 && chown -R app:app /app

EXPOSE 8080

CMD ["./entrypoint.sh"]
```

Caveat: `spacy download` uses pip against the network in the runtime stage, the model is a pure-python wheel, no compiler needed, OK.

- [ ] **Step 4: Verify.** Run the hardening tests: `.venv/bin/python -m pytest tests/test_container_hardening.py -q`. Then, if docker is available (`command -v docker`), do a real build: `docker build -f infra/fly/Dockerfile -t vra-s17-test . 2>&1 | tail -20` (sandbox/network permitting). If docker isn't available, state plainly in the final report that the image builds only statically-verified and must be `fly deploy`-tested before production.

- [ ] **Step 5: Commit**

```bash
git add infra/fly/Dockerfile tests/test_container_hardening.py
git commit -m "build(docker): multi-stage image, no build toolchain in runtime (S17)"
```

---### Task 8: S16 completion, hash-pinned lockfile

Direct deps are `==`-pinned, but transitives still float. Generate a hash lockfile and use it in the image.

**Files:**
- Create: `requirements.lock`
- Modify: `infra/fly/Dockerfile` (builder stage installs from the lock)

- [ ] **Step 1: Generate the lock.** Prefer uv if present, else pip-tools:

```bash
command -v uv && uv pip compile --generate-hashes -o requirements.lock requirements.txt \
  || (.venv/bin/pip install pip-tools && .venv/bin/pip-compile --generate-hashes -o requirements.lock requirements.txt)
```

- [ ] **Step 2: Sanity-check the lock** resolves the pinned versions actually in use:

```bash
grep -E "^pipecat-ai|^anthropic|^fastapi" requirements.lock
```

Expected: same versions as requirements.txt (pipecat-ai 1.2.1, etc.). If resolution conflicts arise that can't be settled in a few minutes, SKIP this task, keep exact pins, and note the deferral in the audit doc (exact `==` pins on direct deps is already a large fraction of the value).

- [ ] **Step 3: Wire into the Dockerfile builder stage:**

```dockerfile
COPY requirements.txt requirements.lock ./
RUN pip wheel --wheel-dir /wheels --require-hashes -r requirements.lock
```

and in the runtime stage install from `requirements.lock` (`pip install --no-index --find-links=/wheels --require-hashes -r requirements.lock`). Update the container-hardening test if it asserts on the install line.

- [ ] **Step 4: Run hardening tests; commit**

```bash
git add requirements.lock infra/fly/Dockerfile tests/test_container_hardening.py
git commit -m "build(deps): hash-pinned lockfile for reproducible images (S16)"
```

---

### Task 9: CI, GitHub Actions (ruff + pytest on push/PR)

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Establish the ruff baseline locally**

Run: `.venv/bin/python -m ruff check tools/ tests/`
If there are pre-existing violations, do NOT mass-fix; scope CI to what passes today (e.g. add `--select` of currently-clean rules or limit paths) and note the debt. If clean, use the plain command.

- [ ] **Step 2: Write `.github/workflows/ci.yml`:**

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 25
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: requirements.txt

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-asyncio ruff
          python -m spacy download en_core_web_sm

      - name: Lint
        run: ruff check tools/ tests/

      - name: Test
        run: python -m pytest tests/ -q
```

Adjust: if pytest/pytest-asyncio are already in requirements.txt, drop the extra install; if Task 8 produced `requirements.lock`, install from it instead for parity with the image.

- [ ] **Step 3: Validate the workflow syntax**, `command -v actionlint && actionlint .github/workflows/ci.yml || .venv/bin/python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`.

- [ ] **Step 4: Run the same commands locally** (install step excepted) to prove the CI body passes: ruff + pytest.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run ruff + pytest on every push and PR"
```

Note for the final report: CI fires when the branch is pushed; the repo is intentionally ~44 commits unpushed, so the user decides when it first runs.

---

### Task 10: Audit doc + memory housekeeping

**Files:**
- Modify: `docs/audit-2026-06-01-security-performance.md` (status column + per-finding headers)
- Modify: `patchnotes.md` (append a 2026-06-10 section)

- [ ] **Step 1: Update the findings table** status column to match verified reality:
  - `✅ done` (pre-existing, stale rows): SC1, S4, S6, S8, S9, S11, S12, SC7, P4, S18, plus S10 (signed `to`/`sig`/`exp` stream params shipped, the per-call-nonce variant deliberately not built), S14 (streaming shipped; explicit tighter /vm limit deliberately not added, global `240/minute` default covers it), S15 → `✅ done` after Task 5, S16 → `✅ done` after Task 8 (or `partial, exact pins only` if Task 8 was skipped).
  - `✅ done 2026-06-10` (this plan): P2, P3, P5, S13, S15, SC6, S17, S16.
  - `deferred (multi-machine only)`: SC2, add one line to its detail section: "Deferred 2026-06-10: demo-path only, single-machine deploy; revisit with stateless HMAC tokens when a 2nd machine ships."
- [ ] **Step 2: Mirror the same status into each finding's `###` detail header** (the `, ✅ DONE …` suffix pattern already used by S1/S2).
- [ ] **Step 3: Append to `patchnotes.md`** a short dated section listing: P2, P3, P5, S13, S15-chunked, SC6, S17, S16-lock, CI added, SC1 verified-fixed, SC2 deferred.
- [ ] **Step 4: Run full suite one last time**, all green.
- [ ] **Step 5: Commit**

```bash
git add docs/audit-2026-06-01-security-performance.md patchnotes.md
git commit -m "docs(audit): sync finding statuses to verified code state; defer SC2"
```

---

## Deliberately NOT in scope
- **SC2** (stateless demo tokens): demo-only, breaks only on multi-machine/restart; deferred until a 2nd machine exists.
- **S14 remainder** (explicit tighter /vm limit): global 240/min/IP default already throttles; adding a per-route limit requires refactoring the Limiter out of `_build_app`, poor cost/benefit now.
- **S10 remainder** (per-call nonce): the shipped signed `to`+`exp` routing token covers the realistic threat; per-call nonce adds state the SC1 fix just removed.
- **GDPR items** (per user: GDPR removed from product scope). gdpr_prefix code paths stay (harmless, tested) unless the user asks for removal.
- **Stripe billing, dashboards, A/B test**, gatekept by client acquisition per user.
