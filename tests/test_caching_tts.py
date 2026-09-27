"""Unit tests for CachingCartesiaTTSService (offline; WS + metrics mocked)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

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


async def _fake_yields_none(self, text, context_id):
    """Stand in for Cartesia's streaming run_tts: yields None only. Audio is
    delivered separately by the (simulated) background receive task, see
    _deliver, which is the real Cartesia ordering."""
    yield None


async def _deliver(svc, ctx, *chunks):
    """Simulate the Cartesia background receive task delivering audio frames to
    the audio context AFTER run_tts returned, ending with TTSStoppedFrame."""
    for c in chunks:
        await svc.append_to_audio_context(
            ctx, TTSAudioRawFrame(audio=c, sample_rate=8000, num_channels=1, context_id=ctx)
        )
    await svc.append_to_audio_context(ctx, TTSStoppedFrame(context_id=ctx))


async def _drive(svc, text, ctx):
    out = []
    async for f in svc.run_tts(text, ctx):
        out.append(f)
    return out


def test_eligibility_and_key():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()
    assert svc._eligible(GREETING) is True
    assert svc._eligible("dynamic caller-specific text") is False
    # key includes resolved voice + sample rate so voice/text changes invalidate
    assert svc._key(GREETING) == (GREETING, "test-voice", 8000)

    off = _make_svc(enabled=False)
    assert off._eligible(GREETING) is False


def test_hit_replays_without_synth():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()
    # Build the key the SAME way run_tts looks it up. Use svc._key(...), it uses
    # _init_sample_rate (8000); svc.sample_rate is 0 here because pipeline start()
    # has not run in this offline unit test.
    key = svc._key(GREETING)
    CachingCartesiaTTSService._cache[key] = [b"AAAA", b"BBBB"]

    appended = []

    async def spy_super_append(self, context_id, frame):
        appended.append(frame)

    # Stub the metric methods directly on the instance (repo uses unittest.mock).
    svc.stop_ttfb_metrics = AsyncMock()
    svc.start_tts_usage_metrics = AsyncMock()
    svc.remove_audio_context = AsyncMock()

    with (
        patch.object(TTSService, "append_to_audio_context", spy_super_append),
        patch.object(
            CartesiaTTSService,
            "run_tts",
            Mock(side_effect=AssertionError("live path must not run on a hit")),
        ),
    ):
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
    svc.stop_ttfb_metrics.assert_awaited_once()   # no dangling TTFB metric
    svc.start_tts_usage_metrics.assert_not_called()  # not billed
    assert yielded == [None]                       # matches live Cartesia yield pattern
    # Hit must CLOSE the audio context (queues the None sentinel that ends the
    # base drain loop), mirrors the live Cartesia path. Omitting it stalls the
    # next turn by ~stop_frame_timeout_s (~3s).
    svc.remove_audio_context.assert_awaited_once_with("ctx1")


def test_miss_captures_then_hits():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()
    svc.stop_ttfb_metrics = AsyncMock()
    svc.start_tts_usage_metrics = AsyncMock()

    async def scenario():
        await _drive(svc, GREETING, "c1")
        # run_tts has returned, but Cartesia delivers audio asynchronously AFTER
        # that. The capture must stay armed until the audio + TTSStoppedFrame
        # arrive, this assertion fails against the pop-in-finally bug.
        assert "c1" in svc._capturing
        await _deliver(svc, "c1", b"ZZ")  # background task delivers audio + stop

    with (
        patch.object(TTSService, "append_to_audio_context", AsyncMock()),
        patch.object(CartesiaTTSService, "run_tts", _fake_yields_none),
    ):
        asyncio.run(scenario())

    key = svc._key(GREETING)
    assert CachingCartesiaTTSService._cache[key] == [b"ZZ"]
    assert svc._capturing == {}

    # Second call: hit -> live path must NOT run; cached audio is replayed.
    replayed = []

    async def spy_append(self, ctx, frame):
        replayed.append(frame)

    with (
        patch.object(TTSService, "append_to_audio_context", spy_append),
        patch.object(
            CartesiaTTSService, "run_tts",
            Mock(side_effect=AssertionError("should be a hit")),
        ),
    ):
        yielded = asyncio.run(_drive(svc, GREETING, "c2"))
    assert yielded == [None]
    assert [type(f).__name__ for f in replayed] == ["TTSAudioRawFrame", "TTSStoppedFrame"]
    assert bytes(replayed[0].audio) == b"ZZ"


def test_non_allowlisted_text_never_cached():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()
    pii = "Sure, I'll text 555-1234 to John at 9pm."

    async def scenario():
        await _drive(svc, pii, "c1")
        assert svc._capturing == {}            # non-eligible text is never armed
        await _deliver(svc, "c1", b"PII-AUDIO")  # even if audio arrives, it's ignored

    with (
        patch.object(TTSService, "append_to_audio_context", AsyncMock()),
        patch.object(CartesiaTTSService, "run_tts", _fake_yields_none),
    ):
        asyncio.run(scenario())
    assert CachingCartesiaTTSService._cache == {}  # PII text is never stored


def test_disabled_flag_never_caches():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc(enabled=False)

    async def scenario():
        await _drive(svc, GREETING, "c1")
        assert svc._capturing == {}            # disabled -> never armed
        await _deliver(svc, "c1", b"ZZ")

    with (
        patch.object(TTSService, "append_to_audio_context", AsyncMock()),
        patch.object(CartesiaTTSService, "run_tts", _fake_yields_none),
    ):
        asyncio.run(scenario())
    assert CachingCartesiaTTSService._cache == {}


def test_error_frame_discards_capture():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()

    async def scenario():
        await _drive(svc, GREETING, "c1")
        assert "c1" in svc._capturing          # armed on the miss
        # Partial audio, then a synthesis error (no TTSStoppedFrame).
        await svc.append_to_audio_context(
            "c1", TTSAudioRawFrame(audio=b"PARTIAL", sample_rate=8000, num_channels=1, context_id="c1")
        )
        await svc.append_to_audio_context("c1", ErrorFrame(error="cartesia error"))

    with (
        patch.object(TTSService, "append_to_audio_context", AsyncMock()),
        patch.object(CartesiaTTSService, "run_tts", _fake_yields_none),
    ):
        asyncio.run(scenario())
    assert CachingCartesiaTTSService._cache == {}   # nothing committed
    assert svc._capturing == {}                     # ErrorFrame cleared the capture


def test_live_generator_error_discards_capture():
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()

    async def _raises(self, text, context_id):
        raise RuntimeError("cartesia died mid-stream")
        yield None  # unreachable; marks this an async generator

    with (
        patch.object(TTSService, "append_to_audio_context", AsyncMock()),
        patch.object(CartesiaTTSService, "run_tts", _raises),
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(_drive(svc, GREETING, "c1"))
    assert CachingCartesiaTTSService._cache == {}   # nothing committed
    assert svc._capturing == {}                     # live-generator error -> finally popped


def test_hit_closes_audio_context_through_real_queue():
    """Integration test against the REAL audio-context machinery.

    Every other test mocks ``append_to_audio_context``, so they cannot see the
    closing ``None`` sentinel that ``remove_audio_context`` appends. Its absence
    was the ~3s pre-reply stall bug: without it the base drain loop blocks for
    ``stop_frame_timeout_s`` before the next turn can start. Here we drive the
    hit path through the genuine per-context ``asyncio.Queue`` (no ``start()``
    needed, the dict/queue are built in ``__init__`` and the drain task only
    runs after ``start()``) and assert the exact queued sequence ends in
    ``None``.
    """
    CachingCartesiaTTSService._cache.clear()
    svc = _make_svc()
    svc.stop_ttfb_metrics = AsyncMock()
    ctx = "ctx-real"

    async def scenario():
        await svc.create_audio_context(ctx)  # real context + real per-context queue
        CachingCartesiaTTSService._cache[svc._key(GREETING)] = [b"AAAA", b"BBBB"]
        async for _ in svc.run_tts(GREETING, ctx):  # real append_to_audio_context
            pass
        q = svc._audio_contexts[ctx]
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return items

    items = asyncio.run(scenario())
    assert [type(f).__name__ for f in items[:3]] == [
        "TTSAudioRawFrame", "TTSAudioRawFrame", "TTSStoppedFrame",
    ]
    assert [bytes(items[0].audio), bytes(items[1].audio)] == [b"AAAA", b"BBBB"]
    assert items[-1] is None  # closing sentinel from remove_audio_context (the close fix)
    assert len(items) == 4
