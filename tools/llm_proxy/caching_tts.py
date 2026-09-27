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
    # Process-wide cache shared across per-call instances. No eviction: size is
    # bounded by the number of distinct cacheable_texts (one greeting per tenant).
    # key: (text, voice, sample_rate) -> ordered list of raw audio byte chunks
    _cache: dict[tuple[str, str | None, int], list[bytes]] = {}

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
        self._capturing: dict[str, tuple[tuple[str, str | None, int], list[bytes]]] = {}

    def _eligible(self, text: str) -> bool:
        return self._cache_enabled and text in self._cacheable_texts

    def _key(self, text: str) -> tuple[str, str | None, int]:
        return (text, self._settings.voice, self._init_sample_rate)

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
                # Close the context: queue the None sentinel that ends the base
                # drain loop (mirrors the live Cartesia path on "done",
                # cartesia/tts.py). Without it the loop blocks for
                # stop_frame_timeout_s (~3s) and stalls the next turn.
                await self.remove_audio_context(context_id)
                yield None  # do NOT yield the audio frames; we appended them directly
                return
            # Miss for a cacheable text: record this context for capture (Task 3
            # adds the capture tee; for now this just marks intent).
            self._capturing[context_id] = (key, [])

        live_completed = False
        try:
            async for frame in super().run_tts(text, context_id):
                yield frame
            live_completed = True
        finally:
            # Audio for a cacheable miss arrives asynchronously (Cartesia's
            # background receive task) AFTER this generator exhausts, and
            # append_to_audio_context finalizes the capture on the delivered
            # TTSStoppedFrame. So a clean exhaustion must LEAVE the capture
            # armed; only discard a still-armed capture if the live generator
            # itself errored or was cancelled (no finalization will ever come).
            if not live_completed:
                self._capturing.pop(context_id, None)

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
