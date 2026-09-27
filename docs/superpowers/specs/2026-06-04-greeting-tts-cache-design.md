# Greeting TTS Cache, Design Spec

**Date:** 2026-06-04
**Status:** Approved design, pending spec review
**Author:** pairing session (nub088 + Claude)

## Problem

The deterministic BC PIPA disclosure greeting is synthesized **live by Cartesia on
every call** (`tools/llm_proxy/voice_pipeline.py:545-557`, via `TTSSpeakFrame`). The
text is fixed per tenant (`_render_pipa_greeting`, `voice_pipeline.py:227`, interpolates
`business.ai_name` + `business.short_name`). Re-synthesizing identical audio every call:

- **Costs** ~270 characters of Cartesia billing per call, on 100% of calls. $0 today
  (Cartesia free tier) but a recurring line item once on a paid plan (~$13/mo per 1,000
  calls on Pro).
- Adds ~200-400 ms first-word TTFB at call start (minor, the `MuteUntilFirstBotComplete`
  strategy holds the caller for the full ~9 s greeting regardless, so this is **not** the
  primary motivation).

**Primary goal: eliminate the recurring per-call greeting synthesis cost.** Latency is a
small secondary benefit. Reliability of the known cold-path call *drops* is explicitly
**out of scope** (those are the Twilio Media Stream WS / 31921, unrelated to Cartesia).

## Non-goals

- Caching any non-static / dynamic / PII-bearing speech. **Hard safety boundary.**
- Cross-deploy / cross-restart persistence (disk or S3). v1 is in-memory only.
- A build-time pre-render CLI tool. (Considered; rejected for v1, see Alternatives.)
- Changing the greeting text, the mute strategy, or the call-start frame sequence.

## Approach

Introduce `CachingCartesiaTTSService(CartesiaTTSService)` and use it in place of
`CartesiaTTSService` in the pipeline. The cache lives *below* the frame lifecycle, so the
base class continues to emit `TTSStartedFrame` / `TTSStoppedFrame`, bot-speaking frames,
the mute-strategy lift, and the LLM-context append unchanged. We only short-circuit the
network synthesis for a known-static utterance.

### Why this shape

Cartesia's streaming service (`run_tts`, `tts.py:707`) sends text to a WebSocket and the
audio returns asynchronously via `_process_messages` (`tts.py:653`) calling
`append_to_audio_context(ctx_id, TTSAudioRawFrame(...))`, ending with a `TTSStoppedFrame`.
`start_tts_usage_metrics(text)` (`tts.py:730`) is the cost/usage meter. Hooking here lets
us (a) replay audio into the same audio-context machinery and (b) skip the WS send + usage
meter on a hit, so a cached greeting is genuinely not billed.

### Components

**1. `CachingCartesiaTTSService`**, new module `tools/llm_proxy/caching_tts.py`.

- Constructor adds `cacheable_texts: frozenset[str] = frozenset()` and
  `cache_enabled: bool = True`. All other args pass through to `CartesiaTTSService`.
- **Process-level cache** (class-attribute dict, reset between unit tests), because a fresh
  TTS instance is built per call (`voice_pipeline.py:469`) and reuse must span calls.
  - Key: `(text, voice_id, sample_rate)` where `voice_id`/`sample_rate` come from resolved
    settings. Changing greeting text, voice, business, or rate → different key → miss → live.
  - Value: `list[bytes]`, the ordered raw audio chunks captured from a live synth.
- **Gating:** a text is eligible only if `cache_enabled and text in cacheable_texts`.
  Non-eligible text is never stored and never replayed, the PII safety boundary.

**Hit path**, override `run_tts(text, context_id)`:
- Precondition (verified, `tts_service.py:1043-1048`): with `push_start_frame=True`, the
  base class has already called `create_audio_context(context_id)`, `start_ttfb_metrics()`,
  and appended `TTSStartedFrame` *before* entering `run_tts`. So the context exists and
  appending is safe.
- If eligible and key present in cache: `await self.stop_ttfb_metrics()` (the live path's
  TTFB stop is the WS `"done"` message at `tts.py:660`, which a hit skips, must stop it
  here or leave a dangling metric); then for each cached chunk,
  `await self.append_to_audio_context(context_id, TTSAudioRawFrame(audio=chunk,
  sample_rate=self.sample_rate, num_channels=1, context_id=context_id))`; then
  `append_to_audio_context(context_id, TTSStoppedFrame(context_id=context_id))`;
  `yield None`; return. **No WS send, no `start_tts_usage_metrics`** → not billed.
  Do **not** also `yield` the audio frames, `tts_process_generator` (`tts_service.py:1091-1093`)
  would append them a second time. Appending directly + `yield None` matches the live
  Cartesia pattern exactly.
- Else: delegate to the live path (below).

**Miss / capture path**, override `append_to_audio_context(context_id, frame)`:
- Tee: while an eligible text is in flight for `context_id`, accumulate
  `frame.audio` bytes from `TTSAudioRawFrame`s into a per-context buffer; on the
  terminal `TTSStoppedFrame`, atomically store `buffer` under the key and clear it.
- Always call `super().append_to_audio_context(...)` so live playback is unaffected.
- To know which `context_id` is "eligible and in flight," `run_tts` records
  `self._capturing[context_id] = key` before delegating to `super().run_tts(...)` on a
  miss, and clears it when capture finalizes or on error.
- Any exception during capture → discard the partial buffer, leave the cache empty for
  that key (next call simply tries again). Never raises into the call.

**2. Pipeline wiring**, `voice_pipeline.py`:
- Compute the greeting once where the TTS is built: `pipa_greeting =
  _render_pipa_greeting(loaded_prompt.config) if loaded_prompt.config.pipa_mode else None`.
- Build `CachingCartesiaTTSService(..., cacheable_texts=frozenset([pipa_greeting]) if
  pipa_greeting else frozenset(), cache_enabled=GREETING_TTS_CACHE)`.
- Reuse that same `pipa_greeting` value in the `on_client_connected` handler (refactor so
  the string is computed in one place) so the registered key matches the spoken text
  exactly.
- `GREETING_TTS_CACHE` env flag (default `True`), parsed near the other env reads. Setting
  it false (no deploy needed beyond env change/restart) fully disables caching → pure live
  behavior, the rollback switch.

### Data flow

```
call N (miss):  on_client_connected → TTSSpeakFrame(greeting)
                  → base: create ctx, TTSStartedFrame
                  → run_tts: record capturing[ctx]=key; super().run_tts (WS send + usage meter)
                  → _process_messages → append_to_audio_context(ctx, AudioRawFrame)  [TEE→buffer]
                  → ... → append_to_audio_context(ctx, TTSStoppedFrame)  [TEE finalize: cache[key]=buffer]
                  → base: bot-speaking lifecycle, mute lift  (unchanged)

call N+1 (hit): on_client_connected → TTSSpeakFrame(greeting)
                  → base: create ctx, TTSStartedFrame
                  → run_tts: key in cache → append cached AudioRawFrames + TTSStoppedFrame to ctx;
                             NO WS send, NO usage meter
                  → base: bot-speaking lifecycle, mute lift  (unchanged, identical to live)
```

### Error handling

- **Capture failure / partial audio:** discard buffer; key stays uncached; call plays live
  normally. Self-heals next call.
- **Cache disabled / unknown text:** identical to current behavior.
- **Concurrency:** asyncio is single-threaded; concurrent calls populating the same key
  write identical bytes (idempotent), last-writer-wins, no lock needed. A read mid-capture
  simply misses → live.
- **Voice/greeting change:** new key → miss → live → re-populate. Old entries become
  unreachable (bounded: at most one entry per distinct (tenant-greeting, voice, rate)).

## Testing (TDD)

Unit tests in `tests/test_caching_tts.py` with a fake transport / mocked WS so no live
Cartesia calls:

1. **Hit replays, no synth:** same eligible text twice → 2nd call issues no WS send and no
   `start_tts_usage_metrics`; appended audio frames byte-match the captured set; TTFB metric
   is stopped (no dangling metric) and a `TTSStoppedFrame` is appended to close the context.
2. **Miss falls through:** eligible text not yet cached → live path runs and audio is teed.
3. **PII / non-allowlisted never cached:** a non-eligible text synthesized twice → cache
   stays empty, live path both times.
4. **Capture failure is silent:** simulated error mid-capture → no raise, key uncached,
   subsequent call still works.
5. **Flag off:** `cache_enabled=False` → never caches even allowlisted text.

Regression: existing `tests/test_voice_pipeline.py` (23 tests) stay green.

Run via `.venv/bin/python -m pytest tests/test_caching_tts.py tests/test_voice_pipeline.py`.

## Alternatives considered

- **Raw-audio injection at call-start** (replace `TTSSpeakFrame` with `OutputAudioRawFrame`
  + hand-rolled bot-speaking/mute lift): puts custom timing logic on the fragile call-start
  path; reimplements lifecycle the base class already owns. Rejected.
- **Build-time `tools/prerender_greeting.py` + disk/volume cache:** WAT-aligned and removes
  the first-call-per-process synth, but must produce byte-identical audio format to the live
  path or playback corrupts, and adds an ops step (re-run on greeting/voice change). Deferred
  as a possible v2 once paid-tier volume justifies the cross-deploy savings; lazy capture is
  format-safe by construction.

## Rollout

1. Land behind `GREETING_TTS_CACHE` (default on).
2. Verify on staging: confirm 2nd call in a warmed process logs a cache hit, greeting audio
   sounds identical, mute/interruption timing unchanged.
3. If anything looks off in prod, set `GREETING_TTS_CACHE=false` and restart, instant revert.
