# BC PIPA Compliance, Voice Receptionist (v0.1)

**Status:** Design, pre-implementation
**Date:** 2026-05-21
**Owner:** Mohd
**Implementation surface:** `tools/llm_proxy/` (voice pipeline, function tools, prompt loader), `tools/redact_pii.py` (wiring), `config/clients/*.json` (mode flag)

## Goal

Bring the non-GDPR call path into honest, defensible compliance with BC's *Personal Information Protection Act* (PIPA) for Canadian client deployments (Clear Slope today; future BC clients on the same pattern). Five changes, all $0 in dollars:

1. **Fix `set_recording_preference` so the opt-out actually does something.** Today the tool records a preference and nothing downstream reads it, the greeting promises a control the system doesn't deliver.
2. **Make the disclosure uninterruptable.** A disclosure that gets cut off didn't legally happen.
3. **Generate the greeting deterministically** (hardcoded text via `TTSSpeakFrame`) rather than through Haiku, so compliance-critical wording can't drift between calls.
4. **Wire `tools/redact_pii.py` into the post-call write path** so human reviewers see redacted transcripts by default.
5. **Add a `pipa_mode` client-config flag and PIPA prompt prefix** mirroring the existing GDPR architecture.

## Non-goals (explicitly out of scope for v0.1)

- GDPR / EU / UK / Swiss work, the existing `gdpr_mode` path stays as-is; this spec touches only the non-GDPR / PIPA path.
- Quebec *Law 25*, different regime, separate spec when a Quebec client appears.
- Audio recording / storage / beeping, no audio is persisted today (per `voice_pipeline.py` and the privacy notice). Out of scope to change that.
- Claims of full anonymization. Redaction reduces re-identification risk; it does not eliminate it. The spec explicitly does *not* assert the redacted output is "non-personal information" under PIPA.
- Cross-border processor obligations (GDPR Article 28 etc.), those are contractual between the developer and the operator, not in scope for the call-path code.
- DSR / retention tooling changes, `tools/data_subject_request.py` and `tools/retention_enforcer.py` already exist and stay as-is.
- New sub-processors. The vendor list is unchanged.

## Grounding

Legal scope (paraphrased, not legal advice):

- **Criminal Code s. 184**, one-party-consent rule already satisfied; the AI is a party to the call.
- **PIPA s. 7**, consent for collection. Implied consent is available for the "obvious purpose" of service delivery (caller calling a pest-control AI to get pest-control service). The QA review of saved transcripts is a *secondary use* and needs a stronger basis.
- **PIPA s. 10(1)**, purposes must be identified before or at the time of collection. The current greeting names "quality and follow-up" but not AI processing, transcript retention, human review, or US sub-processors.
- **PIPA s. 14**, right to withdraw consent. The current `set_recording_preference` tool documents itself as "today it's an audit signal", the right-to-withdraw is not actually plumbed through the retention path. This is the largest exposure.
- **PIPA s. 17**, retention only as long as reasonable. Already addressed by `tools/retention_enforcer.py` and the 90-day window in the privacy notice; no change needed.

Audit findings consolidated from the discussion preceding this spec:

| # | Finding | Severity |
|---|---|---|
| F1 | `set_recording_preference` is a no-op, promised opt-out doesn't gate retention | High |
| F2 | Disclosure can be cut off by an early-speaking caller | Medium-high |
| F3 | Greeting wording is LLM-generated and varies across calls | Medium |
| F4 | Transcripts written to Sheets are not redacted before human review | Medium |
| F5 | Non-GDPR greeting omits US-sub-processor and human-review disclosures | Medium |
| F6 | No verbal pointer to the written privacy notice | Low |

## Component changes

### 1. `config/clients/*.json`, add `pipa_mode`

Add a sibling to `gdpr_mode`:

```json
{
  "client_id": "clearslope",
  "pipa_mode": true,
  ...
}
```

`clearslope.json` gets `"pipa_mode": true`. Default is `false` (a client with neither flag falls back to the current bare prompt path; new BC clients should set the flag explicitly).

**Invariant:** `pipa_mode` and `gdpr_mode` are mutually exclusive. The loader rejects configs where both are true (fail-loud at startup).

### 2. `tools/llm_proxy/client_config.py`, surface the flag

Extend the existing config model with `pipa_mode: bool = False`. The flag propagates through `ClientContext` the same way `gdpr_mode` does today.

### 3. `tools/llm_proxy/voice_pipeline.py`, three changes

**3a. Add `PIPA_PROMPT_PREFIX`** (parallel to `GDPR_PROMPT_PREFIX`):

- Tool-use instructions for `set_recording_preference` ("no transcript" / "stop recording" → `record=false`).
- Tool-use instructions for `human_handoff` (kept available; PIPA best practice, not a hard right).
- Data minimization instructions (don't probe for special-category data; pest triage only).
- No "explicit consent" framing (implied consent is the PIPA basis for service-delivery processing).

`_compose_system_prompt` switches on the flag: `pipa_mode` selects the PIPA prefix, `gdpr_mode` selects the GDPR prefix, neither selects no prefix (unchanged behavior for non-flagged clients).

**3b. Hardcoded greeting via `TTSSpeakFrame`.** Replace the LLM-generated greeting in `on_client_connected` for PIPA-mode clients:

```python
def _render_pipa_greeting(ctx: ClientContext) -> str:
    return (
        f"Hi, this is {ctx.config.business.ai_name} at "
        f"{ctx.config.business.short_name}, I'm AI. Calls are transcribed "
        f"and reviewed by our team to improve service, after personal info "
        f"is scrubbed. Say 'no transcript' if you'd rather we don't keep one. "
        f"Full details at our website's privacy page. What's going on?"
    )

# in on_client_connected, when pipa_mode is set:
greeting = _render_pipa_greeting(client_ctx)
await task.queue_frames([
    LLMMessagesAppendFrame(messages=[{"role": "assistant", "content": greeting}]),
    TTSSpeakFrame(text=greeting),
])
```

Target length: ≤ 9 seconds spoken. The LLM is *not* invoked for this turn, Cartesia receives the text directly, the LLM context aggregator records the assistant turn for history coherence.

**Note on the privacy-page URL.** The greeting refers to "our website's privacy page" rather than hardcoding a domain. The exact URL is per-client and lives in the operator's published privacy policy (which the caller can find via the business's normal website). Hardcoding a URL string the AI reads aloud is fragile and adds another seconds of audio. If a client wants a specific URL spoken, add an optional `business.privacy_url_spoken` config field in a follow-up, out of scope for v0.1.

**3c. Add `STTMuteFilter(FIRST_SPEECH)` to the pipeline.** Inserted between `transport.input()` and `stt`:

```python
from pipecat.processors.filters.stt_mute_filter import (
    STTMuteFilter, STTMuteConfig, STTMuteStrategy,
)

stt_mute = STTMuteFilter(
    config=STTMuteConfig(strategies={STTMuteStrategy.FIRST_SPEECH})
)
```

Applied unconditionally (both PIPA and GDPR modes benefit). The filter mutes user audio during the very first bot utterance so the disclosure plays through, then unmutes for the rest of the call. *Verification step in implementation: confirm `STTMuteFilter` API matches the pinned Pipecat version in `requirements.txt` before committing; the strategy enum name has changed across Pipecat releases.*

### 4. `tools/llm_proxy/function_tools.py`, make `set_recording_preference` functional

Today (lines 344–354): writes to a local `recording_state` dict that no one reads. Fix:

- `recording_state` becomes part of the call's mutable state object that `log_request` reads.
- When `log_request` writes a row, it sets a new column `transcript_retain` to `False` if `recording_state["record"] is False`. Default (no preference expressed) follows the operator's policy → `True` in PIPA mode (implied consent for QA review with redaction), `False` in GDPR mode (already correct under that mode's "explicit consent" default).
- A new column `transcript_retain` (boolean) is added to the Sheet schema. Mirrored in `sheets_writer.format_row`.
- The post-call transcript-purge step (see §5) reads this column to decide whether to persist or purge the transcript artifact.

**Tool description update.** The `set_recording_preference` schema gets a clarification: the tool *gates retention of the saved transcript*, not the audio (which is already not retained). This matters because if the operator's lawyer reads the tool schemas they should match what the system actually does.

### 5. Post-call transcript-write path, wire `tools/redact_pii.py`

Today: `log_request` writes a row to Google Sheets. The conversation transcript itself lives in the LLM context aggregator and is not written to durable storage by the call path (it's reconstructible from the per-call log file, which contains turn-by-turn LLM messages).

Proposed:

- At end-of-call (in the `finally` block of `run_voice_session`, after the elapsed-minute accounting), serialize the full conversation transcript.
- If `recording_state["record"] is False` → skip persistence entirely; only the Sheet row (with the structured intake fields) remains.
- Otherwise: invoke `PIIRedactor.redact_text` from `tools/redact_pii.py` on the transcript, write the redacted version to the existing retention location, and **log the audit residuals as a structured log line** so they're visible in the same logs the team already monitors.
- If audit residuals are non-empty, the transcript file is tagged in its filename or metadata (e.g., `quarantine_` prefix) so any QA review tooling can route them to a more-restricted reviewer tier. v0.1 does not build the tiered-review tooling; it only emits the signal.

**HMAC key requirement.** `PIIRedactor` requires `REDACTION_HMAC_KEY` (32 bytes hex). It is already documented in `.env.example`; the deploy preflight should verify it's set in the Fly secrets before the new write path activates. A missing key fails the call's end-of-call write loudly, *but does not affect the live call*, the caller has already been served.

### 6. Privacy notice (`PRIVACY_NOTICE_DRAFT.md`), base section update

Three textual updates to the base (non-GDPR) clauses:

- "How we use it", add a bullet: "Transcripts of calls are automatically scrubbed of personal information (names, phone numbers, addresses) before any human review."
- "How long we keep it", clarify the 90-day retention applies to redacted transcripts; raw transcripts are not retained beyond the call's end-of-session write.
- "Recording disclosure", replace "may be recorded" with the actual current state: "Calls are transcribed by AI in real time. The transcript is scrubbed of personal information and may be reviewed by our team to improve service. You can decline transcript retention by saying 'no transcript' at any time during the call."

These changes match the greeting text and the new tool behavior. The implementation notes section already disclaims "not legal advice, have a lawyer review before publishing," which stays.

## Data flow (after the change)

```
Caller dials → Twilio → WS → Pipecat transport.input()
                              ↓
                              STTMuteFilter ───(muted for first bot turn)
                              ↓
                              STT (Deepgram)
                              ↓
                              LLMContextAggregator.user()
                              ↓
                              LLM (Anthropic Haiku) ───(NOT invoked for greeting)
                              ↓
                              TTS (Cartesia) ←──── TTSSpeakFrame(greeting) on connect
                              ↓
                              transport.output() → Twilio → Caller

Caller says "no transcript" → set_recording_preference(record=false)
                                        ↓
                            recording_state["record"] = False
                                        ↓ (read at end-of-call)
                            Skip transcript persistence; only Sheet row written

Caller does not opt out → recording_state["record"] is None (default)
                                        ↓
                            PIPA mode default = retain (after redaction)
                                        ↓
                            PIIRedactor.redact_text(transcript)
                                        ↓
                            Audit residuals? → quarantine_<sid>.txt
                                        ↓
                            Otherwise → call_<sid>.txt (redacted, plain)
```

## Error handling

- **Redactor unavailable** (missing `REDACTION_HMAC_KEY`, Presidio model failure, etc.): log loudly, do not write a raw transcript to the retention path, write the Sheet row normally so the operator still has the intake. The transcript is lost, not leaked, this is the correct tradeoff.
- **Anthropic API error during the contextual redaction pass**: the redactor already falls back gracefully to the Presidio-only output (see `_contextual_pass` exception handler in `tools/redact_pii.py:204–216`). Behavior is preserved.
- **Caller says "no transcript" mid-call**: the model calls `set_recording_preference(record=false)`. End-of-call write checks the latest state. The brief in-memory transcript that existed before the opt-out is included in the no-persist decision (it's never written anywhere durable).
- **`STTMuteFilter` import fails** (Pipecat version mismatch): startup error, not a runtime error. The deploy preflight catches it before the first call.
- **Both `pipa_mode` and `gdpr_mode` set in config**: `client_config` loader rejects at startup with a clear error.

## Testing

Unit tests (new):

- `client_config` rejects configs with both flags set.
- `_render_pipa_greeting` interpolates ai_name and short_name correctly; output length ≤ 9 seconds at 150 wpm equivalent (~22 words).
- `set_recording_preference` updates the shared state that `log_request` reads.
- `log_request` with `recording_state["record"] is False` writes `transcript_retain=False` to the Sheet row.

Integration tests (new):

- End-to-end voice session with simulated "no transcript" mid-call: assert no transcript artifact is written to the retention path.
- End-to-end voice session with no opt-out: assert redacted transcript is written and audit residuals are emitted as a log line.
- End-to-end with audit residuals present: assert quarantine filename prefix is applied.

Manual verification (not automatable):

- Call the Fly preview with a real phone, verify the greeting plays for full duration even when speaking immediately ("can you hear me?" within the first second should not interrupt).
- Verify the spoken greeting is identical across three consecutive calls (determinism check).
- Verify saying "no transcript" mid-call results in no transcript file on disk for that call_sid.

## Rollout

Single PR, single deploy. Order of edits within the PR:

1. `config/clients/clearslope.json`, add `pipa_mode: true` (does nothing until the loader changes ship, harmless if rolled back).
2. `tools/llm_proxy/client_config.py`, accept and propagate the flag.
3. `tools/llm_proxy/voice_pipeline.py`, `PIPA_PROMPT_PREFIX`, `_render_pipa_greeting`, hardcoded greeting branch, `STTMuteFilter`.
4. `tools/llm_proxy/function_tools.py`, make `set_recording_preference` functional, update `log_request` to consume it.
5. `tools/llm_proxy/sheets_writer.py`, add `transcript_retain` column.
6. End-of-call transcript-write path, invoke `PIIRedactor`, emit audit residuals.
7. `PRIVACY_NOTICE_DRAFT.md`, textual updates to base clauses.

No database migration. The new Sheet column is additive; existing rows show blank in that cell (interpreted as "policy default" by future tooling, but v0.1 has no future tooling that reads it for historical rows).

Rollback: revert the PR. `pipa_mode: false` (or absence) restores the pre-change behavior.

## Open questions deferred to follow-up specs

- **Tiered QA review tooling** (the consumer of quarantine-flagged transcripts), v0.1 only emits the signal.
- **Per-client `privacy_url_spoken`** for the greeting, out of scope; current wording uses "our website's privacy page" which the operator can resolve via their published policy.
- **Caller-location-based mode routing** (Twilio CGN → PIPA, EU CGN → GDPR), out of scope; today the mode is operator-level, set per client config. Cross-jurisdiction routing is a separate spec.
- **Anthropic ZDR confirmation**, contractual, not code; documented as a deploy preflight checklist item.
