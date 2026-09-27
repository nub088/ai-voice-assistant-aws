"""Smoke tests for the voice pipeline factory.

Instantiating a real pipeline requires live network connections to Deepgram,
Cartesia, etc., so these tests only verify:

  - The module imports cleanly with all Pipecat deps available.
  - The receptionist model is locked to Haiku 4.5 (project memory invariant).
  - The system prompt composer prepends a time-context line.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pipecat", reason="pipecat-ai not installed in this environment")


def test_receptionist_model_is_haiku() -> None:
    from tools.llm_proxy.voice_pipeline import RECEPTIONIST_MODEL

    assert RECEPTIONIST_MODEL.startswith("claude-haiku-"), (
        f"call path must use Haiku, got {RECEPTIONIST_MODEL!r}"
    )


def test_compose_system_prompt_is_cache_stable_and_time_free() -> None:
    """P5: the composed system prompt must be byte-stable across calls so the
    ~8.5k-token block gets Anthropic cache READS instead of a cache WRITE per
    call. The volatile time line is injected as a separate context message."""
    from dataclasses import replace

    from tools.llm_proxy.client_config import load_client_config
    from tools.llm_proxy.prompt_loader import LoadedPrompt
    from tools.llm_proxy.voice_pipeline import _compose_system_prompt

    config = replace(load_client_config(), gdpr_mode=False, pipa_mode=False)
    loaded = LoadedPrompt(
        text="System prompt body.",
        version="v0-test",
        prompt_path=Path("/dev/null"),
        taxonomy_path=Path("/dev/null"),
        config=config,
    )
    composed = _compose_system_prompt(loaded)
    assert "[Current time:" not in composed
    assert "System prompt body." in composed
    assert composed == _compose_system_prompt(loaded)


def test_time_context_is_hour_granular() -> None:
    """P5: the time line sits at the prompt-cache lookup boundary; minute
    precision would change it every call and defeat cross-call cache reuse.
    The model only needs day + approximate hour + open/closed status."""
    import re

    from tools.llm_proxy.client_config import load_client_config
    from tools.llm_proxy.prompt_loader import time_context

    tc = time_context(load_client_config())
    assert re.search(r"around \d{1,2} (AM|PM)", tc), tc
    assert not re.search(r"\d:\d", tc), f"minute precision defeats caching: {tc}"
    assert "business hours" in tc or "after hours" in tc


def _loaded_prompt_with(mode: str) -> object:
    """Build a LoadedPrompt for testing the prompt composer in each mode.

    Loads the real GDPR/PIPA prefix files via the public load_prompt() path so
    the composer is exercised against the same artifacts production uses.
    """
    from dataclasses import replace
    from tools.llm_proxy.client_config import load_client_config
    from tools.llm_proxy.prompt_loader import load_prompt

    base = load_client_config()
    if mode == "pipa":
        base = replace(base, pipa_mode=True, gdpr_mode=False)
    elif mode == "gdpr":
        base = replace(base, pipa_mode=False, gdpr_mode=True)
    elif mode == "none":
        base = replace(base, pipa_mode=False, gdpr_mode=False)
    else:
        raise ValueError(mode)
    loaded = load_prompt(config=base)
    # Replace the (long) real prompt body with a marker so test assertions
    # against "System prompt body." remain compact.
    return replace(loaded, text="System prompt body.")


def test_compose_system_prompt_includes_pipa_prefix_when_flagged() -> None:
    from tools.llm_proxy.voice_pipeline import _compose_system_prompt

    composed = _compose_system_prompt(_loaded_prompt_with("pipa"))
    # Marker text from tools/prompts/pipa_prefix.md.
    assert "BC PIPA mode" in composed
    assert "System prompt body." in composed


def test_compose_system_prompt_gdpr_still_uses_gdpr_prefix() -> None:
    from tools.llm_proxy.voice_pipeline import _compose_system_prompt

    composed = _compose_system_prompt(_loaded_prompt_with("gdpr"))
    # Marker text from tools/prompts/gdpr_prefix.md.
    assert "GDPR" in composed
    assert "Article 22" in composed


def test_compose_system_prompt_no_mode_no_prefix() -> None:
    from tools.llm_proxy.voice_pipeline import _compose_system_prompt

    composed = _compose_system_prompt(_loaded_prompt_with("none"))
    # Neither compliance marker should appear when no mode is set.
    assert "BC PIPA mode" not in composed
    assert "Article 22" not in composed
    assert "System prompt body." in composed


def test_render_pipa_greeting_interpolates_business_and_ai_name() -> None:
    from tools.llm_proxy.voice_pipeline import _render_pipa_greeting

    loaded = _loaded_prompt_with("pipa")
    text = _render_pipa_greeting(loaded.config)
    assert "Sarah" in text
    assert "Clear Slope" in text
    assert "recorded" in text          # PIPA recording disclosure
    assert "virtual assistant" in text  # AI self-identification


def test_render_pipa_greeting_fits_short_utterance_window() -> None:
    """≤9 seconds spoken ≈ ≤35 words at 150 wpm conversational pace."""
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


def test_persist_redacted_transcript_logs_redacted_text(caplog, monkeypatch) -> None:
    import logging
    import tools.llm_proxy.voice_pipeline as vp

    # REDACTION_HMAC_KEY is required by load_default_redactor; supply a fixed
    # test value and reset the module-level redactor cache (both the redactor
    # itself and the init sentinel) so this test is deterministic regardless
    # of run order.
    monkeypatch.setenv("REDACTION_HMAC_KEY", "00" * 32)
    monkeypatch.setattr(vp, "_REDACTOR", None)
    monkeypatch.setattr(vp, "_REDACTOR_INITIALIZED", False)

    messages = [
        {"role": "assistant", "content": "Hi, this is Sarah."},
        {"role": "user", "content": "Hi, I'm Alex Doe at 604-555-1234."},
    ]
    caplog.set_level(logging.INFO, logger="voice_pipeline")
    vp._persist_redacted_transcript(
        call_sid="CA300",
        messages=messages,
        retain=True,
        client_id="clearslope",
        duration_sec=0.0,
        caller_number_last4=None,
    )
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
    _persist_redacted_transcript(
        call_sid="CA301",
        messages=messages,
        retain=False,
        client_id="clearslope",
        duration_sec=0.0,
        caller_number_last4=None,
    )
    transcript_lines = [
        r.getMessage() for r in caplog.records if "REDACTED_TRANSCRIPT" in r.getMessage()
    ]
    assert transcript_lines == []
    # A skip note is emitted so we can audit opt-out compliance.
    skip_lines = [
        r.getMessage() for r in caplog.records if "transcript_retain=false" in r.getMessage()
    ]
    assert len(skip_lines) == 1


def test_persist_redacted_transcript_writes_to_store(monkeypatch, tmp_path) -> None:
    """End-of-call persistence should call transcript_store.save with the
    redacted text and metadata threaded from run_voice_session's scope.
    """
    from tools.llm_proxy import transcript_store, voice_pipeline
    from tools.redact_pii import RedactionResult

    # Avoid the expensive Presidio init, substitute a fake redactor.
    class _FakeRedactor:
        def redact_text(self, text: str) -> RedactionResult:
            return RedactionResult(
                redacted_text="[redacted]: " + text,
                counts={"NAME": 1},
                residuals=[],
            )

    monkeypatch.setattr(voice_pipeline, "_REDACTOR", _FakeRedactor())
    monkeypatch.setattr(voice_pipeline, "_REDACTOR_INITIALIZED", True)

    captured: dict[str, object] = {}

    def _fake_save(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(transcript_store, "save", _fake_save)

    voice_pipeline._persist_redacted_transcript(
        call_sid="CA_wired",
        messages=[
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
        retain=True,
        assistant_label="Sarah",
        client_id="clearslope",
        duration_sec=12.3,
        caller_number_last4="5678",
    )

    assert captured["call_sid"] == "CA_wired"
    assert captured["client_id"] == "clearslope"
    assert captured["duration_sec"] == 12.3
    assert captured["caller_number_last4"] == "5678"
    assert "hello" in captured["redacted_text"]
    assert captured["placeholder_counts"] == {"NAME": 1}
    assert captured["residual_count"] == 0
    assert captured["quarantine"] is False


def test_persist_redacted_transcript_skips_when_not_retained(monkeypatch) -> None:
    """retain=False short-circuits before any save call."""
    from tools.llm_proxy import transcript_store, voice_pipeline

    called = False

    def _fake_save(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(transcript_store, "save", _fake_save)

    voice_pipeline._persist_redacted_transcript(
        call_sid="CA_optout",
        messages=[{"role": "user", "content": "no thanks"}],
        retain=False,
        assistant_label="Sarah",
        client_id="clearslope",
        duration_sec=1.0,
        caller_number_last4=None,
    )
    assert called is False


# --- Latency instrumentation (item 1): per-turn TTFB metrics ---------------

def test_build_pipeline_params_enables_metrics() -> None:
    """The call pipeline must collect metrics so per-leg TTFB is observable."""
    from tools.llm_proxy.voice_pipeline import _build_pipeline_params

    params = _build_pipeline_params(8000)
    assert params.enable_metrics is True
    assert params.enable_usage_metrics is True
    # One TTFB per leg per turn, not one per streamed TTS chunk (clean signal).
    assert params.report_only_initial_ttfb is True
    # μ-law 8 kHz both ways must be preserved (matches Twilio Media Streams).
    assert params.audio_in_sample_rate == 8000
    assert params.audio_out_sample_rate == 8000


def test_ttfb_log_lines_includes_only_ttfb_metrics() -> None:
    """The TTFB formatter emits one line per TTFB metric and ignores others."""
    from pipecat.metrics.metrics import ProcessingMetricsData, TTFBMetricsData
    from tools.llm_proxy.voice_pipeline import _ttfb_log_lines

    data = [
        TTFBMetricsData(processor="DeepgramSTTService", value=0.100, model=None),
        ProcessingMetricsData(processor="DeepgramSTTService", value=0.500),
        TTFBMetricsData(processor="CartesiaTTSService", value=0.250, model="sonic-3.5"),
    ]
    lines = _ttfb_log_lines(data, "CA9")
    assert len(lines) == 2
    assert any("processor=DeepgramSTTService" in ln and "ttfb_ms=100" in ln for ln in lines)
    tts_line = next(ln for ln in lines if "CartesiaTTSService" in ln)
    assert "ttfb_ms=250" in tts_line
    assert "sonic-3.5" in tts_line
    # call_sid is embedded so the JSON log formatter extracts it as a field.
    assert all("call_sid=CA9" in ln for ln in lines)


@pytest.mark.asyncio
async def test_ttfb_log_observer_logs_ttfb_on_metrics_frame(caplog) -> None:
    import logging
    from types import SimpleNamespace

    from pipecat.frames.frames import MetricsFrame
    from pipecat.metrics.metrics import TTFBMetricsData
    from tools.llm_proxy.voice_pipeline import CallMetricsObserver

    observer = CallMetricsObserver(call_sid="CA42")
    frame = MetricsFrame(
        data=[TTFBMetricsData(processor="AnthropicLLMService", value=0.321, model="claude-haiku-4-5")]
    )
    caplog.set_level(logging.INFO, logger="voice_pipeline")
    await observer.on_push_frame(SimpleNamespace(frame=frame, timestamp=1))

    lines = [r.getMessage() for r in caplog.records if "voice_ttfb" in r.getMessage()]
    assert len(lines) == 1
    assert "call_sid=CA42" in lines[0]
    assert "processor=AnthropicLLMService" in lines[0]
    assert "ttfb_ms=321" in lines[0]


@pytest.mark.asyncio
async def test_ttfb_log_observer_ignores_non_metrics_frames(caplog) -> None:
    import logging
    from types import SimpleNamespace

    from pipecat.frames.frames import TTSSpeakFrame
    from tools.llm_proxy.voice_pipeline import CallMetricsObserver

    observer = CallMetricsObserver(call_sid="CA43")
    caplog.set_level(logging.INFO, logger="voice_pipeline")
    await observer.on_push_frame(SimpleNamespace(frame=TTSSpeakFrame(text="hi"), timestamp=1))
    assert [r for r in caplog.records if "voice_ttfb" in r.getMessage()] == []


# --- Per-call usage accumulation (cost instrumentation, Phase 2) -----------

@pytest.mark.asyncio
async def test_call_metrics_observer_accumulates_llm_and_tts_usage() -> None:
    """The observer sums Anthropic token counts and Cartesia characters across
    frames, exposing them as the kwargs save_call_usage takes. total_tokens is
    never stored (it excludes cache tokens)."""
    from types import SimpleNamespace

    from pipecat.frames.frames import MetricsFrame
    from pipecat.metrics.metrics import (
        LLMTokenUsage,
        LLMUsageMetricsData,
        TTFBMetricsData,
        TTSUsageMetricsData,
    )
    from tools.llm_proxy.voice_pipeline import CallMetricsObserver

    observer = CallMetricsObserver(call_sid="CA_usage")
    llm1 = LLMUsageMetricsData(
        processor="AnthropicLLMService", model="claude-haiku-4-5",
        value=LLMTokenUsage(
            prompt_tokens=100, completion_tokens=40, total_tokens=140,
            cache_read_input_tokens=900, cache_creation_input_tokens=50,
        ),
    )
    tts = TTSUsageMetricsData(processor="CartesiaTTSService", model="sonic", value=320)
    # A TTFB datum rides along, must not disturb the usage tally.
    await observer.on_push_frame(SimpleNamespace(
        frame=MetricsFrame(data=[llm1, tts, TTFBMetricsData(processor="x", value=0.1, model=None)]),
        timestamp=1,
    ))
    # A second, distinct frame adds more LLM usage.
    llm2 = LLMUsageMetricsData(
        processor="AnthropicLLMService", model="claude-haiku-4-5",
        value=LLMTokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    await observer.on_push_frame(SimpleNamespace(frame=MetricsFrame(data=[llm2]), timestamp=2))

    totals = observer.usage_totals()
    assert totals["llm_prompt_tokens"] == 110
    assert totals["llm_completion_tokens"] == 45
    assert totals["llm_cache_read_tokens"] == 900
    assert totals["llm_cache_creation_tokens"] == 50
    assert totals["tts_characters"] == 320
    assert totals["llm_model"] == "claude-haiku-4-5"
    assert totals["tts_model"] == "sonic"
    assert "llm_total_tokens" not in totals  # excludes cache → never stored


@pytest.mark.asyncio
async def test_call_metrics_observer_dedups_same_frame_id() -> None:
    """The same MetricsFrame seen twice (e.g. downstream + upstream) counts once."""
    from types import SimpleNamespace

    from pipecat.frames.frames import MetricsFrame
    from pipecat.metrics.metrics import LLMTokenUsage, LLMUsageMetricsData
    from tools.llm_proxy.voice_pipeline import CallMetricsObserver

    observer = CallMetricsObserver(call_sid="CA_dedup")
    frame = MetricsFrame(data=[LLMUsageMetricsData(
        processor="AnthropicLLMService", model="claude-haiku-4-5",
        value=LLMTokenUsage(prompt_tokens=100, completion_tokens=40, total_tokens=140),
    )])
    await observer.on_push_frame(SimpleNamespace(frame=frame, timestamp=1))
    await observer.on_push_frame(SimpleNamespace(frame=frame, timestamp=2))
    assert observer.usage_totals()["llm_prompt_tokens"] == 100


@pytest.mark.asyncio
async def test_call_metrics_observer_zero_usage_when_no_metrics() -> None:
    """A call with no LLM/TTS frames (e.g. early hangup) yields all-zero totals."""
    from tools.llm_proxy.voice_pipeline import CallMetricsObserver

    totals = CallMetricsObserver(call_sid="CA_empty").usage_totals()
    assert totals["llm_prompt_tokens"] == 0
    assert totals["tts_characters"] == 0
    assert totals["llm_model"] is None


# --- End-of-call persistence offloaded off the event loop (item 7) ---------

@pytest.mark.asyncio
async def test_persist_redacted_transcript_async_runs_off_the_event_loop(monkeypatch) -> None:
    """End-of-call redaction (CPU-heavy Presidio) must not block the asyncio
    loop, or one call's teardown stalls other concurrent calls' audio and new
    call setup. The async wrapper runs the sync persist in a worker thread."""
    import threading

    import tools.llm_proxy.voice_pipeline as vp

    recorded: dict[str, object] = {}

    def _stub(**kwargs):
        recorded["thread"] = threading.current_thread()
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(vp, "_persist_redacted_transcript", _stub)

    await vp._persist_redacted_transcript_async(
        call_sid="CA_async",
        messages=[{"role": "user", "content": "hi"}],
        retain=True,
        assistant_label="Sarah",
        client_id="clearslope",
        duration_sec=3.0,
        caller_number_last4="1234",
    )

    # Ran on a thread-pool worker, not the event-loop (main) thread.
    assert recorded["thread"] is not threading.main_thread()
    # All kwargs were threaded through unchanged.
    assert recorded["kwargs"]["call_sid"] == "CA_async"
    assert recorded["kwargs"]["client_id"] == "clearslope"
    assert recorded["kwargs"]["caller_number_last4"] == "1234"
    assert recorded["kwargs"]["retain"] is True


# --- PII never leaks to the durable store or logs (audit S1 + S2) ----------

def test_persist_masks_residual_pii_and_omits_content_from_logs(caplog, monkeypatch) -> None:
    """S1: residual (audit-detected, un-scrubbed) PII must be masked before it
    reaches SQLite. S2: transcript content must never be written to the log
    stream, only redaction metadata."""
    import logging

    from tools.llm_proxy import transcript_store, voice_pipeline
    from tools.redact_pii import RedactionResult

    class _FakeRedactor:
        def redact_text(self, text: str) -> RedactionResult:
            # Simulates the quarantine case: a spelled-/typed-out number the
            # NER + contextual passes missed survives in redacted_text and is
            # only flagged by the audit pass.
            return RedactionResult(
                redacted_text="Caller: my number is 6042701401",
                counts={"NAME": 1},
                residuals=[{"label": "long_number_run", "text": "6042701401", "offset": 21}],
            )

    monkeypatch.setattr(voice_pipeline, "_REDACTOR", _FakeRedactor())
    monkeypatch.setattr(voice_pipeline, "_REDACTOR_INITIALIZED", True)

    captured: dict[str, object] = {}
    monkeypatch.setattr(transcript_store, "save", lambda **kw: captured.update(kw))

    caplog.set_level(logging.INFO, logger="voice_pipeline")
    voice_pipeline._persist_redacted_transcript(
        call_sid="CA_resid",
        messages=[{"role": "user", "content": "hi"}],
        retain=True,
        assistant_label="Sarah",
        client_id="clearslope",
        duration_sec=1.0,
        caller_number_last4=None,
    )

    # S1, the stored body must not contain the residual PII.
    assert "6042701401" not in captured["redacted_text"]
    assert "[REDACTED]" in captured["redacted_text"]
    assert captured["quarantine"] is True
    assert captured["residual_count"] == 1

    # S2, no transcript content (and certainly no raw PII) in the log stream.
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "6042701401" not in joined
    assert "content=" not in joined
    # Redaction metadata is still emitted for observability.
    line = next(m for m in (r.getMessage() for r in caplog.records) if "REDACTED_TRANSCRIPT" in m)
    assert "residuals=1" in line


# --- VAD must reach the aggregator, not the transport (audit P4) ------------

def test_user_aggregator_params_wire_vad_analyzer() -> None:
    """P4: in Pipecat 1.2.1 the VAD analyzer is read from
    LLMUserAggregatorParams (the aggregator builds a VADController only if it's
    set). The transport's `vad_analyzer=` kwarg is silently dropped (not a field
    of FastAPIWebsocketParams), so VAD/interruptions only engage if it's wired
    in here."""
    from tools.llm_proxy.voice_pipeline import _build_user_aggregator_params

    sentinel = object()
    params = _build_user_aggregator_params(vad_analyzer=sentinel, mute_strategies=[])
    assert params.vad_analyzer is sentinel


# --- GREETING_TTS_CACHE flag + CachingCartesiaTTSService wiring (Task 5) ----

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
    try:
        assert vp.GREETING_TTS_CACHE is False
    finally:
        # Restore the module to its default-env state unconditionally, so an
        # assertion failure here can't leave GREETING_TTS_CACHE=False for the
        # rest of the suite.
        monkeypatch.delenv("GREETING_TTS_CACHE", raising=False)
        importlib.reload(vp)


def test_pipeline_uses_caching_tts_subclass() -> None:
    from tools.llm_proxy.caching_tts import CachingCartesiaTTSService
    from pipecat.services.cartesia.tts import CartesiaTTSService
    assert issubclass(CachingCartesiaTTSService, CartesiaTTSService)
