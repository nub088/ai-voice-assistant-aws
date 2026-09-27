# Greeting TTS Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop re-synthesizing the fixed PIPA greeting on every call by caching its audio in-process and replaying it, skipping Cartesia's WebSocket send + usage meter on a hit.

**Architecture:** A `CachingCartesiaTTSService(CartesiaTTSService)` subclass. For allowlisted static text only (the greeting), it replays captured audio frames into Cartesia's audio-context machinery (`append_to_audio_context`) and skips synthesis; the base class keeps owning every lifecycle frame, the mute lift, and the LLM-context append. Lazy in-process capture, automatic live fallback on any miss/error, `GREETING_TTS_CACHE` kill-switch.

**Tech Stack:** Python 3.14, pipecat-ai 1.2.1 (`CartesiaTTSService`, `TTSService` audio contexts), pytest + `unittest.mock` (`AsyncMock`, `patch`, `monkeypatch`).

**Spec:** `docs/superpowers/specs/2026-06-04-greeting-tts-cache-design.md`
**Branch:** continue on `security-perf-hardening` (current), work is part of the perf-hardening theme.

> **Post-implementation amendments** (this plan was updated to match what shipped):
> - **Mocking:** tests use `unittest.mock` (the repo convention), NOT `pytest-mock`/`mocker`, `pytest-mock` is not a project dependency. Code blocks below reflect this.
> - **Cache key:** `_key()` uses `self._init_sample_rate` (the constructed value), not `self.sample_rate`, because `sample_rate` is `0` until the pipeline's async `start()` runs. Tests build keys via `svc._key(...)`.
> - **Async-delivery timing (critical):** Cartesia delivers audio via a background task *after* `run_tts` exhausts, so capture cleanup uses a `live_completed` guard (see Task 2) and the Task 3 test models that ordering. An early draft's unconditional `finally` pop silently broke capture.
> - **Defensive copy:** the cache commit stores `list(chunks)` (immutable snapshot).
> - **Flag test:** `test_greeting_tts_cache_flag_can_disable` restores module state in a `try/finally`.
> - **Source of truth for tests:** the Task 3-4 test code blocks below were written before the async-delivery fix and use helper names from an earlier draft. The shipped tests in `tests/test_caching_tts.py` (helpers `_fake_yields_none` + `_deliver`, which model the background-task ordering) are authoritative.

---

## File Structure

- **Create:** `tools/llm_proxy/caching_tts.py`, the `CachingCartesiaTTSService` subclass. One responsibility: cache/replay TTS for allowlisted text.
- **Create:** `tests/test_caching_tts.py`, unit tests for the subclass (offline, mocked WS).
- **Modify:** `tools/llm_proxy/voice_pipeline.py`, swap `CartesiaTTSService` → `CachingCartesiaTTSService`, compute the greeting once, add the `GREETING_TTS_CACHE` flag.

Key facts verified against pipecat source (cite in case of confusion during impl):
- Base creates the audio context + calls `start_ttfb_metrics()` + appends `TTSStartedFrame` *before* `run_tts` runs (`tts_service.py:1043-1048`).
- Live audio arrives via the receive task calling `append_to_audio_context(ctx, TTSAudioRawFrame(...))`, ending with `append_to_audio_context(ctx, TTSStoppedFrame(...))` (`cartesia/tts.py:653-682`).
- `start_tts_usage_metrics(text)` is the cost meter (`cartesia/tts.py:730`); `stop_ttfb_metrics()` is normally called on the WS `"done"` (`cartesia/tts.py:660`).
- `append_to_audio_context` / `create_audio_context` are defined on `TTSService` (`tts_service.py:1206/1216`); `CartesiaTTSService(WebsocketTTSService(TTSService,...))` inherits them.

---

## Task 1: Scaffold the caching subclass (constructor + eligibility/key)

**Files:**
- Create: `tools/llm_proxy/caching_tts.py`
- Test: `tests/test_caching_tts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_caching_tts.py
"""Unit tests for CachingCartesiaTTSService (offline; WS + metrics mocked)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat", reason="pipecat-ai not installed in this environment")

from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame, TTSStoppedFrame
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.tts_service import TTSService

from tools.llm_proxy.caching_tts import CachingCartesiaTTSService

GREETING = "Hi, this is Sarah, a virtual assistant on behalf of Acme. How can I help you today?"


def _make_svc(cacheable=frozenset({GREETING}), enabled=True):
    svc = CachingCartesiaTTSService(
        api_key="test-key",
        voice_id="test-voice",
        sample_rate=8000,
        cacheable_texts=cacheable,
        cache_enabled=enabled,
    )
    return svc


def test_eligibility_and_key():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()
    assert svc._eligible(GREETING) is True
    assert svc._eligible("dynamic caller-specific text") is False
    # key includes resolved voice + sample rate so voice/text changes invalidate
    assert svc._key(GREETING) == (GREETING, "test-voice", 8000)

    off = _make_svc(enabled=False)
    assert off._eligible(GREETING) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_caching_tts.py::test_eligibility_and_key -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'tools.llm_proxy.caching_tts'`

- [ ] **Step 3: Write minimal implementation**

```python
# tools/llm_proxy/caching_tts.py
"""A CartesiaTTSService that replays cached audio for known-static utterances.

Only texts in ``cacheable_texts`` (the per-tenant PIPA greeting) are ever cached
or replayed. Dynamic / PII-bearing speech is always synthesized live. On a cache
hit the WebSocket send and the usage meter are skipped, so the utterance is not
billed.
"""
from __future__ import annotations

from typing import AsyncGenerator

from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame, TTSStoppedFrame
from pipecat.services.cartesia.tts import CartesiaTTSService


class CachingCartesiaTTSService(CartesiaTTSService):
    # Process-wide cache shared across per-call instances.
    # key: (text, voice, sample_rate) -> ordered list of raw audio byte chunks
    _cache: dict[tuple[str, object, int], list[bytes]] = {}

    def __init__(
        self,
        *args,
        cacheable_texts: frozenset[str] = frozenset(),
        cache_enabled: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._cacheable_texts = cacheable_texts
        self._cache_enabled = cache_enabled
        # context_id -> (key, [chunks]) while a cacheable utterance is in flight
        self._capturing: dict[str, tuple[tuple[str, object, int], list[bytes]]] = {}

    def _eligible(self, text: str) -> bool:
        return self._cache_enabled and text in self._cacheable_texts

    def _key(self, text: str) -> tuple[str, object, int]:
        return (text, self._settings.voice, self.sample_rate)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_caching_tts.py::test_eligibility_and_key -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/caching_tts.py tests/test_caching_tts.py
git commit -m "feat(tts): scaffold CachingCartesiaTTSService (eligibility + cache key)"
```

---

## Task 2: Hit path, replay cached audio, skip synthesis

**Files:**
- Modify: `tools/llm_proxy/caching_tts.py`
- Test: `tests/test_caching_tts.py`

- [ ] **Step 1: Write the failing test**

```python
def test_hit_replays_without_synth(mocker):
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()
    key = (GREETING, svc._settings.voice, svc.sample_rate)
    CachingCartesiaTTSService._cache[key] = [b"AAAA", b"BBBB"]

    appended = []

    async def spy_super_append(self, context_id, frame):  # noqa: ANN001
        appended.append(frame)

    mocker.patch.object(TTSService, "append_to_audio_context", new=spy_super_append)
    stop_ttfb = mocker.patch.object(svc, "stop_ttfb_metrics", new=AsyncMock())
    start_usage = mocker.patch.object(svc, "start_tts_usage_metrics", new=AsyncMock())
    # If the live path were taken this would raise:
    mocker.patch.object(
        CartesiaTTSService, "run_tts",
        new=mocker.Mock(side_effect=AssertionError("live path must not run on a hit")),
    )

    async def drive():
        out = []
        async for f in svc.run_tts(GREETING, "ctx1"):
            out.append(f)
        return out

    yielded = asyncio.run(drive())

    assert [type(f).__name__ for f in appended] == [
        "TTSAudioRawFrame", "TTSAudioRawFrame", "TTSStoppedFrame",
    ]
    assert [bytes(appended[0].audio), bytes(appended[1].audio)] == [b"AAAA", b"BBBB"]
    stop_ttfb.assert_awaited_once()          # no dangling TTFB metric
    start_usage.assert_not_called()          # not billed
    assert yielded == [None]                 # matches live Cartesia yield pattern
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_caching_tts.py::test_hit_replays_without_synth -v`
Expected: FAIL, `AttributeError`/`TypeError`: `run_tts` not defined on the subclass yet (inherited live one runs).

- [ ] **Step 3: Write minimal implementation** (append `run_tts` to the class)

```python
    async def run_tts(
        self, text: str, context_id: str
    ) -> AsyncGenerator[Frame | None, None]:
        if self._eligible(text):
            key = self._key(text)
            cached = self._cache.get(key)
            if cached is not None:
                # Hit. The base already started the TTFB metric (tts_service.py
                # :1045); the live path's stop is the WS "done" we are skipping,
                # so stop it here. Replay audio into the context and close it.
                await self.stop_ttfb_metrics()
                for chunk in cached:
                    await self.append_to_audio_context(
                        context_id,
                        TTSAudioRawFrame(
                            audio=chunk,
                            sample_rate=self.sample_rate,
                            num_channels=1,
                            context_id=context_id,
                        ),
                    )
                await self.append_to_audio_context(
                    context_id, TTSStoppedFrame(context_id=context_id)
                )
                yield None  # do NOT yield the audio frames; we appended them directly
                return
            # Miss for a cacheable text: record this context for capture.
            self._capturing[context_id] = (key, [])

        live_completed = False
        try:
            async for frame in super().run_tts(text, context_id):
                yield frame
            live_completed = True
        finally:
            # Audio for a cacheable miss arrives ASYNCHRONOUSLY (Cartesia's
            # background receive task) AFTER this generator exhausts, and
            # append_to_audio_context finalizes the capture on the delivered
            # TTSStoppedFrame. So a clean exhaustion must LEAVE the capture
            # armed; only discard a still-armed capture if the live generator
            # itself errored or was cancelled (no finalization will ever come).
            if not live_completed:
                self._capturing.pop(context_id, None)
```

> **Critical (found in code review, folded in here):** an earlier draft popped
> `_capturing` unconditionally in `finally`. Because Cartesia delivers audio via
> a background task *after* `run_tts` exhausts, that popped the capture buffer
> before any audio arrived, capture silently failed every call. The
> `live_completed` guard above is the fix; the Task 3 test models the async
> delivery ordering so this can't regress.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_caching_tts.py::test_hit_replays_without_synth -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/caching_tts.py tests/test_caching_tts.py
git commit -m "feat(tts): replay cached greeting audio on hit (skip WS + usage meter)"
```

---

## Task 3: Miss + tee capture, then hit on the second call

**Files:**
- Modify: `tools/llm_proxy/caching_tts.py`
- Test: `tests/test_caching_tts.py`

- [ ] **Step 1: Write the failing test**

Module-level helpers (define once near the top of the test module; reused by Tasks 3-4). Uses `unittest.mock` to match the repo convention, `pytest-mock`/`mocker` is NOT a project dependency:

```python
async def _fake_live_run_tts(self, text, context_id):
    """Stand in for the live WS path: deliver audio via the audio context the
    way cartesia._process_messages does, then finish."""
    await self.append_to_audio_context(
        context_id,
        TTSAudioRawFrame(audio=b"ZZ", sample_rate=8000, num_channels=1, context_id=context_id),
    )
    await self.append_to_audio_context(context_id, TTSStoppedFrame(context_id=context_id))
    yield None


async def _drive(svc, text, ctx):
    out = []
    async for f in svc.run_tts(text, ctx):
        out.append(f)
    return out


def test_miss_captures_then_hits():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()
    svc.stop_ttfb_metrics = AsyncMock()
    svc.start_tts_usage_metrics = AsyncMock()

    # First call: miss -> live (faked) -> tee captures the audio chunk.
    with (
        patch.object(TTSService, "append_to_audio_context", AsyncMock()),
        patch.object(CartesiaTTSService, "run_tts", _fake_live_run_tts),
    ):
        asyncio.run(_drive(svc, GREETING, "c1"))

    key = svc._key(GREETING) # use _key(), svc.sample_rate is 0 pre-start
    assert CachingCartesiaTTSService._cache[key] == [b"ZZ"]
    assert svc._capturing == {}  # finalized + cleared

    # Second call: hit -> live path must NOT run.
    with (
        patch.object(TTSService, "append_to_audio_context", AsyncMock()),
        patch.object(
            CartesiaTTSService,
            "run_tts",
            Mock(side_effect=AssertionError("should be a hit")),
        ),
    ):
        yielded = asyncio.run(_drive(svc, GREETING, "c2"))
    assert yielded == [None]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_caching_tts.py::test_miss_captures_then_hits -v`
Expected: FAIL, `KeyError` on `_cache[key]` (tee not implemented; nothing captured).

- [ ] **Step 3: Write minimal implementation** (append `append_to_audio_context` override)

```python
    async def append_to_audio_context(self, context_id: str, frame: Frame) -> None:
        cap = self._capturing.get(context_id)
        if cap is not None:
            key, chunks = cap
            if isinstance(frame, TTSAudioRawFrame):
                chunks.append(bytes(frame.audio))
            elif isinstance(frame, TTSStoppedFrame):
                # Clean end of a cacheable utterance: commit and stop recording.
                self._cache[key] = list(chunks)  # own an immutable snapshot
                self._capturing.pop(context_id, None)
            elif isinstance(frame, ErrorFrame):
                # Synthesis error mid-capture: discard the partial buffer.
                self._capturing.pop(context_id, None)
        await super().append_to_audio_context(context_id, frame)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_caching_tts.py::test_miss_captures_then_hits -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/caching_tts.py tests/test_caching_tts.py
git commit -m "feat(tts): tee-capture greeting audio on miss; reuse on next call"
```

---

## Task 4: Safety, never cache non-allowlisted text, partial audio, or when disabled

**Files:**
- Test: `tests/test_caching_tts.py` (no implementation change expected, these assert existing behavior)

- [ ] **Step 1: Write the failing tests**

```python
def test_non_allowlisted_text_never_cached():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()
    with (
        patch.object(TTSService, "append_to_audio_context", AsyncMock()),
        patch.object(CartesiaTTSService, "run_tts", _fake_live_run_tts),
    ):
        asyncio.run(_drive(svc, "Sure, I'll text 555-1234 to John at 9pm.", "c1"))
        asyncio.run(_drive(svc, "Sure, I'll text 555-1234 to John at 9pm.", "c2"))
    assert CachingCartesiaTTSService._cache == {}  # PII text is never stored


def test_disabled_flag_never_caches():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc(enabled=False)
    with (
        patch.object(TTSService, "append_to_audio_context", AsyncMock()),
        patch.object(CartesiaTTSService, "run_tts", _fake_live_run_tts),
    ):
        asyncio.run(_drive(svc, GREETING, "c1"))
    assert CachingCartesiaTTSService._cache == {}


def test_partial_audio_is_discarded():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()

    async def _fake_live_errors(self, text, context_id):
        # Audio chunk arrives, then the synth fails before TTSStoppedFrame.
        await self.append_to_audio_context(
            context_id,
            TTSAudioRawFrame(audio=b"PARTIAL", sample_rate=8000, num_channels=1, context_id=context_id),
        )
        raise RuntimeError("cartesia died mid-stream")
        yield None  # unreachable; marks this an async generator

    with (
        patch.object(TTSService, "append_to_audio_context", AsyncMock()),
        patch.object(CartesiaTTSService, "run_tts", _fake_live_errors),
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(_drive(svc, GREETING, "c1"))
    assert CachingCartesiaTTSService._cache == {}   # nothing committed
    assert svc._capturing == {}                     # capture cleared by finally
```

- [ ] **Step 2: Run tests to verify they pass** (behavior already implemented in Tasks 1-3)

Run: `.venv/bin/python -m pytest tests/test_caching_tts.py -v`
Expected: All PASS. If `test_partial_audio_is_discarded` fails because the buffer survived, confirm the `finally: self._capturing.pop(...)` from Task 2 is present.

- [ ] **Step 3: Commit**

```bash
git add tests/test_caching_tts.py
git commit -m "test(tts): assert PII/partial/disabled never populate the greeting cache"
```

---

## Task 5: Wire the caching service into the pipeline behind a flag

**Files:**
- Modify: `tools/llm_proxy/voice_pipeline.py` (imports near top; `TWILIO_SAMPLE_RATE` block ~line 87; TTS build ~line 469-473; `on_client_connected` ~line 546-549)
- Test: `tests/test_voice_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_voice_pipeline.py
def test_greeting_tts_cache_flag_defaults_true(monkeypatch) -> None:
    import importlib
    monkeypatch.delenv("GREETING_TTS_CACHE", raising=False)
    import tools.llm_proxy.voice_pipeline as vp
    importlib.reload(vp)
    assert vp.GREETING_TTS_CACHE is True


def test_greeting_tts_cache_flag_can_disable(monkeypatch) -> None:
    import importlib
    monkeypatch.setenv("GREETING_TTS_CACHE", "false")
    import tools.llm_proxy.voice_pipeline as vp
    importlib.reload(vp)
    assert vp.GREETING_TTS_CACHE is False
    monkeypatch.delenv("GREETING_TTS_CACHE", raising=False)
    importlib.reload(vp)  # restore default for other tests


def test_pipeline_uses_caching_tts_subclass() -> None:
    from tools.llm_proxy.caching_tts import CachingCartesiaTTSService
    from pipecat.services.cartesia.tts import CartesiaTTSService
    assert issubclass(CachingCartesiaTTSService, CartesiaTTSService)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_voice_pipeline.py::test_greeting_tts_cache_flag_defaults_true -v`
Expected: FAIL, `AttributeError: module ... has no attribute 'GREETING_TTS_CACHE'`

- [ ] **Step 3: Write the implementation**

In `tools/llm_proxy/voice_pipeline.py`, add the import alongside the other service imports:

```python
from tools.llm_proxy.caching_tts import CachingCartesiaTTSService
```

Add the flag constant near `TWILIO_SAMPLE_RATE = 8000` (~line 87):

```python
# Replay the fixed PIPA greeting from an in-process audio cache instead of
# re-synthesizing it every call. Set GREETING_TTS_CACHE=false (+ restart) to
# fall back to pure live synthesis.
GREETING_TTS_CACHE = os.environ.get("GREETING_TTS_CACHE", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
```

Replace the TTS construction (currently ~line 469-473). Compute the greeting once so the registered cache key matches the spoken text exactly:

```python
    pipa_greeting = (
        _render_pipa_greeting(loaded_prompt.config)
        if loaded_prompt.config.pipa_mode
        else None
    )
    tts = CachingCartesiaTTSService(
        api_key=cartesia_api_key,
        voice_id=cartesia_voice_id,
        sample_rate=TWILIO_SAMPLE_RATE,
        cacheable_texts=frozenset([pipa_greeting]) if pipa_greeting else frozenset(),
        cache_enabled=GREETING_TTS_CACHE,
    )
```

In `on_client_connected` (~line 546-549), reuse the already-computed greeting instead of re-rendering it:

```python
        if loaded_prompt.config.pipa_mode:
            # Deterministic disclosure: no LLM in the loop. The mute strategy
            # on the user aggregator blocks interruption until TTS finishes.
            greeting = pipa_greeting  # computed once where the TTS is built
            await task.queue_frames(
                [
                    LLMMessagesAppendFrame(
                        messages=[{"role": "assistant", "content": greeting}]
                    ),
                    TTSSpeakFrame(text=greeting),
                ]
            )
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_voice_pipeline.py -v`
Expected: All PASS (new flag tests + existing 23).

- [ ] **Step 5: Commit**

```bash
git add tools/llm_proxy/voice_pipeline.py tests/test_voice_pipeline.py
git commit -m "feat(tts): use CachingCartesiaTTSService for the PIPA greeting (GREETING_TTS_CACHE)"
```

---

## Task 6: Full regression + spec/plan commit

**Files:** none (verification)

- [ ] **Step 1: Run the full suite**

Run: `.venv/bin/python -m pytest tests/test_caching_tts.py tests/test_voice_pipeline.py -v`
Expected: all green. Then a broader sweep for safety:
Run: `.venv/bin/python -m pytest -q`
Expected: no new failures vs. the pre-change baseline.

- [ ] **Step 2: Commit the spec + plan docs** (if not already tracked)

```bash
git add docs/superpowers/specs/2026-06-04-greeting-tts-cache-design.md \
        docs/superpowers/plans/2026-06-04-greeting-tts-cache.md
git commit -m "docs(tts): greeting TTS cache design + implementation plan"
```

- [ ] **Step 3: Manual staging verification (human-in-the-loop, before prod)**

Not automatable here, note for the operator:
1. On staging, place two calls into the same warmed machine. Confirm logs show a live greeting synth on call 1 and a cache hit (no Cartesia greeting synth, no usage chars) on call 2.
2. Confirm the greeting audio sounds identical and the mute/interruption timing at call start is unchanged.
3. Confirm `GREETING_TTS_CACHE=false` + restart reverts to live synthesis.

---

## Self-Review Notes

- **Spec coverage:** caching subclass (T1-3), PII/partial/disabled safety boundary (T4), pipeline wiring + flag (T5), regression + manual staging check (T6). All spec sections mapped.
- **Type consistency:** `_cache` key `(text, voice, sample_rate)`, value `list[bytes]`; `_capturing[context_id] = (key, chunks)`; `cacheable_texts: frozenset[str]`; `cache_enabled: bool`. Used identically across `run_tts`, `append_to_audio_context`, and the wiring.
- **Known offline-test assumption:** constructing `CachingCartesiaTTSService` performs no network I/O (`CartesiaTTSService.__init__` only sets attributes; connection is lazy in `run_tts`). If a base `__init__` arg turns out to be required, pass it in `_make_svc`, do not add network setup to tests.
