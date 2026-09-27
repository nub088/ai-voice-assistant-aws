"""
Pipecat voice-pipeline factory for the receptionist.

`run_voice_session()` is invoked once per inbound Twilio Media Stream call by
`voice_routes.py`. It wires:

    Twilio WebSocket
      → TwilioFrameSerializer (μ-law 8 kHz)
      → DeepgramSTTService (streaming)
      → context aggregator (user side)
      → AnthropicLLMService or AWSBedrockLLMService
      → CartesiaTTSService (streaming, 8 kHz)
      → TwilioFrameSerializer (back to caller)
      → context aggregator (assistant side)

Function tools (`log_request`, `end_call`) are registered on the LLM service
with closures bound to this call's `sheets_writer` + `call_sid`. The system
prompt is composed at call start (loaded prompt + time context + tool
instructions) so each call sees the correct business-hours framing.

Fly defaults to Claude Haiku 4.5; the AWS deployment selects a Bedrock model.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import math
import os
import time
from typing import Any

from fastapi import WebSocket

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndTaskFrame,
    LLMMessagesAppendFrame,
    LLMRunFrame,
    MetricsFrame,
    TTSSpeakFrame,
)
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.observers.base_observer import BaseObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_mute.mute_until_first_bot_complete_user_mute_strategy import (
    MuteUntilFirstBotCompleteUserMuteStrategy,
)
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from .client_config import ClientConfig, ClientContext
from .function_tools import TOOLS_SCHEMA, RETRIEVE_KNOWLEDGE_SCHEMA, make_handlers
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from .prompt_loader import LoadedPrompt, time_context
from tools.llm_proxy.caching_tts import CachingCartesiaTTSService

# redact_pii depends on presidio_analyzer + spaCy, which are slow to import
# and may not be installed in lightweight test environments. We import lazily
# inside _get_redactor() so module load never fails without those deps.
# The TYPE_CHECKING guard gives mypy the correct type without a runtime import.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from tools.redact_pii import PIIRedactor

logger = logging.getLogger("voice_pipeline")

# Fly default; AWS sets BEDROCK_MODEL_ID in the ECS task environment.
RECEPTIONIST_MODEL = os.environ.get("BEDROCK_MODEL_ID") or "claude-haiku-4-5-20251001"


def _make_llm():
    """Use Bedrock with the ECS task role when configured; retain Fly defaults."""
    model = os.environ.get("BEDROCK_MODEL_ID", "").strip()
    if model:
        from pipecat.services.aws.llm import AWSBedrockLLMService

        return AWSBedrockLLMService(
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            settings=AWSBedrockLLMService.Settings(
                model=model,
                max_tokens=512,
                enable_prompt_caching=os.environ.get("BEDROCK_PROMPT_CACHE") == "1",
            ),
        )
    return AnthropicLLMService(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        settings=AnthropicLLMService.Settings(
            model=RECEPTIONIST_MODEL,
            enable_prompt_caching=True,
            max_tokens=512,
        ),
    )

# Twilio Media Streams are μ-law 8 kHz.
TWILIO_SAMPLE_RATE = 8000

# Replay the fixed PIPA greeting from an in-process audio cache instead of
# re-synthesizing it every call. Set GREETING_TTS_CACHE=false (+ restart) to
# fall back to pure live synthesis.
GREETING_TTS_CACHE = os.environ.get("GREETING_TTS_CACHE", "1").strip().lower() in (
    "1", "true", "yes", "on",
)


def _build_pipeline_params(sample_rate: int) -> PipelineParams:
    """PipelineParams for a call, with metrics collection enabled.

    `enable_metrics`/`enable_usage_metrics` make each service emit a
    `MetricsFrame` carrying per-leg TTFB (STT/LLM/TTS) plus LLM token and TTS
    character usage. `CallMetricsObserver` turns the TTFB data into one
    grep-able log line per turn (so the dominant latency leg is visible in Fly
    logs) and accumulates the token/char usage for the end-of-call cost row.
    Defaults are off upstream, so this is what actually surfaces the numbers.
    """
    return PipelineParams(
        audio_in_sample_rate=sample_rate,
        audio_out_sample_rate=sample_rate,
        enable_metrics=True,
        enable_usage_metrics=True,
        report_only_initial_ttfb=True,
    )


def _ttfb_log_lines(metrics_data: list, call_sid: str) -> list[str]:
    """Render TTFB metrics into structured log lines (one per TTFB datum).

    Non-TTFB metrics (processing time, token usage, …) are ignored, TTFB
    is the latency signal we tune against. `call_sid=` is embedded so the
    JSON log formatter lifts it into a queryable field.
    """
    lines: list[str] = []
    for m in metrics_data:
        if isinstance(m, TTFBMetricsData):
            lines.append(
                f"voice_ttfb call_sid={call_sid} processor={m.processor} "
                f"model={m.model or '-'} ttfb_ms={(m.value or 0.0) * 1000:.0f}"
            )
    return lines


class CallMetricsObserver(BaseObserver):
    """Routes TTFB to the app logger and accumulates per-call usage totals.

    Two jobs, both off the audio hot path:
      1. TTFB, Pipecat's built-in MetricsLogObserver emits via loguru at DEBUG,
         which bypasses this app's JSON-to-stdout logging. This observer instead
         routes TTFB into the `voice_pipeline` logger so it lands in the JSON Fly
         log stream, tagged with call_sid (one grep-able line per turn).
      2. Usage, it sums the LLM token counts (Anthropic) and TTS characters
         (Cartesia) that Pipecat already emits (enable_usage_metrics=True) so the
         end-of-call finally block can persist one measured `call_usage` row.

    Accumulators are plain ints mutated only on the event loop (single-threaded),
    so no lock is needed. `total_tokens` is intentionally NOT summed: Pipecat
    defines it as prompt+completion (excludes cache tokens), so it's a misleading
    half-count the cost model never uses.
    """

    def __init__(self, *, call_sid: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._call_sid = call_sid
        self._seen: set[int] = set()
        self._llm_prompt = 0
        self._llm_completion = 0
        self._llm_cache_read = 0
        self._llm_cache_creation = 0
        self._llm_model: str | None = None
        self._tts_chars = 0
        self._tts_model: str | None = None

    async def on_push_frame(self, data: Any) -> None:
        frame = data.frame
        if not isinstance(frame, MetricsFrame):
            return
        if frame.id in self._seen:
            return
        self._seen.add(frame.id)
        for m in frame.data:
            if isinstance(m, LLMUsageMetricsData):
                u = m.value
                self._llm_prompt += u.prompt_tokens or 0
                self._llm_completion += u.completion_tokens or 0
                self._llm_cache_read += u.cache_read_input_tokens or 0
                self._llm_cache_creation += u.cache_creation_input_tokens or 0
                self._llm_model = self._llm_model or m.model
            elif isinstance(m, TTSUsageMetricsData):
                self._tts_chars += m.value or 0
                self._tts_model = self._tts_model or m.model
        for line in _ttfb_log_lines(frame.data, self._call_sid):
            logger.info(line)

    def usage_totals(self) -> dict[str, object]:
        """Per-call usage as the exact kwargs `transcript_store.save_call_usage` takes."""
        return {
            "llm_model": self._llm_model,
            "llm_prompt_tokens": self._llm_prompt,
            "llm_completion_tokens": self._llm_completion,
            "llm_cache_read_tokens": self._llm_cache_read,
            "llm_cache_creation_tokens": self._llm_cache_creation,
            "tts_model": self._tts_model,
            "tts_characters": self._tts_chars,
        }


def _build_user_aggregator_params(
    *, vad_analyzer: Any, mute_strategies: list
) -> LLMUserAggregatorParams:
    """Build the user-aggregator params with the VAD analyzer wired in (P4).

    In Pipecat 1.2.1 the VAD analyzer is read from
    LLMUserAggregatorParams.vad_analyzer, the aggregator constructs its
    VADController only when this is set, and that controller drives speech
    start/stop, interruptions, and the `vad_user_speaking` signal the
    smart-turn endpointer relies on. Passing `vad_analyzer=` to the transport
    params (as the old code did) is a no-op: it's not a field of
    FastAPIWebsocketParams, so pydantic silently drops it and VAD never runs.
    """
    return LLMUserAggregatorParams(
        vad_analyzer=vad_analyzer,
        user_mute_strategies=mute_strategies,
    )


def _compose_system_prompt(loaded: LoadedPrompt) -> str:
    """Compose the per-call system prompt from the loaded prompt + the
    compliance-mode prefix (if any) + the runtime time context.

    Prefix text lives in tools/prompts/{gdpr,pipa}_prefix.md and is loaded
    + interpolated by prompt_loader.load_prompt(); the prefix content is
    folded into LoadedPrompt.version so any legal text change is auditable
    by version label alone.

    The volatile time line is NOT part of this block (P5): it's injected as a
    separate context message so this ~8.5k-token block stays byte-stable
    across calls and Anthropic prompt caching gets reads, not a write per
    call. The compliance prefix is per-tenant-stable, so it may stay here.
    """
    if loaded.config.gdpr_mode and loaded.gdpr_prefix:
        prefix = loaded.gdpr_prefix.rstrip() + "\n\n"
    elif loaded.config.pipa_mode and loaded.pipa_prefix:
        prefix = loaded.pipa_prefix.rstrip() + "\n\n"
    else:
        prefix = ""
    return f"{prefix}{loaded.text}"


def _render_pipa_greeting(config: ClientConfig) -> str:
    """Hardcoded BC PIPA disclosure greeting. Deterministic, no LLM in the loop.

    Spoken via TTSSpeakFrame so every caller hears the exact same disclosure.
    Length target: ≤35 words / ≤9 seconds at 150 wpm. Uninterruptable via
    the user-mute strategy on the context aggregator.
    """
    return (
        f"Hi, this is {config.business.ai_name}, a virtual assistant on behalf of "
        f"{config.business.short_name}. This call may be recorded for quality purposes. "
        f"How can I help you today?"
    )


_REDACTOR: "PIIRedactor | None" = None
_REDACTOR_INITIALIZED = False


def _get_redactor() -> "PIIRedactor | None":
    """Lazy-init a process-wide redactor; None if config is missing.

    Build is expensive (Presidio model + spaCy model load), so we cache.
    Cache both the success and failure case via `_REDACTOR_INITIALIZED`,     otherwise a missing REDACTION_HMAC_KEY would re-trigger the expensive
    load on every end-of-call cleanup.
    """
    global _REDACTOR, _REDACTOR_INITIALIZED
    if _REDACTOR_INITIALIZED:
        return _REDACTOR
    try:
        from tools.redact_pii import load_default_redactor
        _REDACTOR = load_default_redactor()
    except Exception:
        logger.exception("redactor unavailable; transcript persistence disabled")
        _REDACTOR = None
    finally:
        _REDACTOR_INITIALIZED = True
    return _REDACTOR


def _extract_transcript_text(
    messages: list[dict[str, Any]],
    *,
    assistant_label: str = "Assistant",
) -> str:
    """Render the user/assistant turns of an LLMContext as a plain transcript.

    Drops system messages, tool calls, and tool results. Only text turns are
    emitted so the redactor sees the actual conversation. `assistant_label`
    is typically the client's `ai_name` so transcripts read naturally.
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
            label = assistant_label if role == "assistant" else "Caller"
            lines.append(f"{label}: {text}")
    return "\n".join(lines)


def _persist_redacted_transcript(
    *,
    call_sid: str,
    messages: list[dict[str, Any]],
    retain: bool,
    assistant_label: str = "Assistant",
    client_id: str,
    duration_sec: float,
    caller_number_last4: str | None,
) -> None:
    """Emit the redacted transcript for this call as a log line + SQLite row.

    Two persistence surfaces:
      1. Structured log line (REDACTED_TRANSCRIPT prefix), grep-friendly,
         kept as a safety net during the SQLite rollout.
      2. Durable SQLite row via transcript_store.save(), the actual store.

    When `retain=False`, both are skipped and an opt-out audit line is
    logged.
    """
    if not retain:
        logger.info("call_sid=%s transcript_retain=false; skipping persistence", call_sid)
        return

    raw = _extract_transcript_text(messages, assistant_label=assistant_label)
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

    # S1: the audit pass only DETECTS residual PII, the redacted_text still
    # contains it. Mask those spans before anything is persisted or logged so
    # un-scrubbed PII (e.g. a number read digit-by-digit) never reaches the
    # durable store. Only the quarantine path needs this, so the redact_pii
    # import (which pulls in Presidio) stays off the clean-call hot path.
    if result.residuals:
        from tools.redact_pii import mask_residuals
        safe_text = mask_residuals(result.redacted_text, result.residuals)
    else:
        safe_text = result.redacted_text

    quarantine_prefix = "QUARANTINE_" if result.residuals else ""
    # Single structured line, keep it grep-friendly. S2: never log transcript
    # content to stdout/Fly logs (no deletion path, broad operator access). The
    # SQLite row written below is the system of record; log only metadata.
    logger.info(
        "%sREDACTED_TRANSCRIPT call_sid=%s residuals=%d counts=%s len=%d",
        quarantine_prefix,
        call_sid,
        len(result.residuals),
        result.counts,
        len(safe_text),
    )

    # Durable store, best-effort, never raises into the call path.
    from tools.llm_proxy import transcript_store
    transcript_store.save(
        call_sid=call_sid,
        client_id=client_id,
        redacted_text=safe_text,
        placeholder_counts=dict(result.counts),
        residual_labels=[r["label"] for r in result.residuals],
        residual_count=len(result.residuals),
        quarantine=bool(result.residuals),
        duration_sec=duration_sec,
        caller_number_last4=caller_number_last4,
    )


async def _persist_redacted_transcript_async(**kwargs: Any) -> None:
    """Run the (CPU-heavy, synchronous) Presidio redaction + SQLite write in a
    worker thread so it never blocks the asyncio event loop.

    The redaction runs in the end-of-call `finally` on the same loop that
    serves every other live call's audio frames and new-call WS setup. Doing
    it inline freezes those for the redaction's duration (tens to hundreds of
    ms on shared-cpu-1x). Offloading keeps the loop responsive, spaCy's NER
    releases the GIL during native inference, so the thread genuinely overlaps
    the loop rather than just deferring the stall.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, functools.partial(_persist_redacted_transcript, **kwargs)
    )


async def run_voice_session(
    *,
    websocket: WebSocket,
    stream_sid: str,
    call_sid: str,
    client_ctx: ClientContext,
    minute_counters: dict[str, float],
    active_calls: dict[str, int] | None = None,
    caller_number: str = "",
) -> None:
    """Run the Pipecat pipeline for a single Twilio call.

    Returns when the call ends (caller hangs up, model invokes `end_call`,
    or pipeline errors). Minute accounting runs in the finally block (D-10):
    wall-clock seconds are converted via math.ceil to whole minutes and added
    to minute_counters[client_id], even if the pipeline raises.

    Timing instrumentation: logs `setup_ms` (entry → runner.run) and
    `greeted_ms` (entry → caller-greeting kickoff) once per call so cold-path
    upstream connection cost is visible in Fly logs alongside Twilio 31921
    failure timestamps. See call_drops_diagnosis.txt.
    """
    t_session_in = time.perf_counter()
    loaded_prompt = client_ctx.loaded_prompt
    sheets_writer = client_ctx.sheets_writer
    sms_notifier = client_ctx.sms_notifier

    twilio_account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token = client_ctx.auth_token  # per-client auth_token sourced from ClientContext (D-12)
    deepgram_api_key = os.environ.get("DEEPGRAM_API_KEY", "")
    cartesia_api_key = os.environ.get("CARTESIA_API_KEY", "")
    cartesia_voice_id = os.environ.get("CARTESIA_VOICE_ID", "").strip()

    if not deepgram_api_key or not cartesia_api_key or not cartesia_voice_id:
        logger.error(
            "call_sid=%s missing required keys: deepgram=%s cartesia=%s voice_id=%s",
            call_sid,
            bool(deepgram_api_key),
            bool(cartesia_api_key),
            bool(cartesia_voice_id),
        )
        await websocket.close(code=1011)
        return

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=twilio_account_sid,
        auth_token=twilio_auth_token,
    )

    # One VAD analyzer per call (per-call state must not be shared across
    # concurrent callers). Wired into the aggregator below, NOT here, see
    # _build_user_aggregator_params (P4).
    vad_analyzer = SileroVADAnalyzer()

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    stt = DeepgramSTTService(api_key=deepgram_api_key)

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

    llm = _make_llm()

    system_prompt = _compose_system_prompt(loaded_prompt)
    context = LLMContext(
        messages=[
            {"role": "system", "content": system_prompt},
            # P5: the volatile time line rides OUTSIDE the cached system block.
            # Pipecat's anthropic adapter extracts only the initial system
            # message as the API `system` param and converts later system-role
            # messages to user role, so the big static block above stays a
            # cross-call-stable cache prefix and this line sits right at the
            # cache lookup boundary (hour-granular, see prompt_loader).
            {"role": "system", "content": time_context(loaded_prompt.config)},
        ],
        tools=(
            ToolsSchema(standard_tools=[*TOOLS_SCHEMA.standard_tools, RETRIEVE_KNOWLEDGE_SCHEMA])
            if os.environ.get(f"{client_ctx.config.env_prefix}_BEDROCK_KNOWLEDGE_BASE_ID")
            else TOOLS_SCHEMA
        ),
    )
    user_mute_strategies = []
    if loaded_prompt.config.pipa_mode or loaded_prompt.config.gdpr_mode:
        user_mute_strategies.append(MuteUntilFirstBotCompleteUserMuteStrategy())

    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=_build_user_aggregator_params(
            vad_analyzer=vad_analyzer, mute_strategies=user_mute_strategies
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    # Hoisted to a named local (was inline in `observers=`) so the end-of-call
    # finally block can read its accumulated usage totals.
    metrics_observer = CallMetricsObserver(call_sid=call_sid)
    task = PipelineTask(
        pipeline,
        params=_build_pipeline_params(TWILIO_SAMPLE_RATE),
        observers=[metrics_observer],
    )

    async def end_call_signal() -> None:
        await task.queue_frame(EndTaskFrame())

    handlers = make_handlers(
        sheets_writer=sheets_writer,
        call_sid=call_sid,
        on_end_call=end_call_signal,
        sms_notifier=sms_notifier,
        caller_number=caller_number,
        client_id=client_ctx.config.client_id,
        knowledge_base_id=os.environ.get(f"{client_ctx.config.env_prefix}_BEDROCK_KNOWLEDGE_BASE_ID", ""),
    )
    recording_state = handlers.pop("_recording_state")
    for name, handler in handlers.items():
        llm.register_function(name, handler)

    @transport.event_handler("on_client_connected")
    async def _on_connected(transport_inst: Any, client: Any) -> None:
        logger.info(
            "call_sid=%s client connected; kicking off greeting (greeted_ms=%.0f)",
            call_sid, (time.perf_counter() - t_session_in) * 1000,
        )

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

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(transport_inst: Any, client: Any) -> None:
        logger.info("call_sid=%s client disconnected", call_sid)
        await task.queue_frame(EndTaskFrame())

    runner = PipelineRunner(handle_sigint=False)
    logger.info(
        "voice_session_timing call_sid=%s setup_ms=%.0f",
        call_sid, (time.perf_counter() - t_session_in) * 1000,
    )
    t0 = time.monotonic()
    client_id_for_reservation = client_ctx.config.client_id
    try:
        # SC5: count this streaming call against the client's ceiling for the
        # whole call. Paired with the finally decrement so it can't leak.
        if active_calls is not None:
            active_calls[client_id_for_reservation] = (
                active_calls.get(client_id_for_reservation, 0) + 1
            )
        await runner.run(task)
    except Exception:
        logger.exception("call_sid=%s pipeline error", call_sid)
    finally:
        if active_calls is not None:
            active_calls[client_id_for_reservation] = max(
                0, active_calls.get(client_id_for_reservation, 0) - 1
            )
        elapsed_sec = time.monotonic() - t0
        elapsed_min = math.ceil(elapsed_sec / 60)
        client_id = client_ctx.config.client_id
        new_total = minute_counters.get(client_id, 0.0) + elapsed_min
        minute_counters[client_id] = new_total
        logger.info(
            "call_sid=%s client_id=%s elapsed_sec=%.1f minutes_charged=%d total_minutes=%.0f",
            call_sid, client_id, elapsed_sec, elapsed_min, new_total,
        )
        # Persist the updated counter so a Fly restart can't erase mid-period
        # usage. Best-effort: errors are swallowed inside save_minute_counter.
        # Offloaded to a worker thread so this synchronous SQLite write never
        # blocks the event loop that serves other live calls.
        from tools.llm_proxy import transcript_store
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            functools.partial(
                transcript_store.save_minute_counter,
                client_id=client_id,
                total_minutes=new_total,
            ),
        )

        # Persist measured per-call usage (LLM tokens + TTS chars) accumulated by
        # the metrics observer, so cost_per_call.py can upgrade Anthropic/Cartesia
        # from modeled to measured. Best-effort + offloaded, like the counter above.
        try:
            await loop.run_in_executor(
                None,
                functools.partial(
                    transcript_store.save_call_usage,
                    call_sid=call_sid,
                    client_id=client_id,
                    duration_sec=elapsed_sec,
                    **metrics_observer.usage_totals(),
                ),
            )
        except Exception:
            logger.exception("call_sid=%s call_usage flush failed", call_sid)

        # End-of-call redacted-transcript log line. Reads the latest
        # recording_state to honor mid-call opt-outs. In gdpr_mode the
        # default is no-retain unless caller affirmed; in pipa_mode (and
        # bare mode) the default is retain. Redaction (Presidio/spaCy) is
        # CPU-heavy and synchronous, so it runs in a worker thread to keep
        # the event loop free for concurrent calls and new-call WS setup.
        pref = recording_state.get("record")
        if pref is None:
            retain = not client_ctx.config.gdpr_mode
        else:
            retain = bool(pref)
        await _persist_redacted_transcript_async(
            call_sid=call_sid,
            messages=list(context.messages),
            retain=retain,
            assistant_label=client_ctx.config.business.ai_name,
            client_id=client_ctx.config.client_id,
            duration_sec=elapsed_sec,
            caller_number_last4=caller_number[-4:] if caller_number else None,
        )
