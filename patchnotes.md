# Patch Notes, Security / Performance / Scalability Hardening

**Branch:** `security-perf-hardening` · **Date:** 2026-06-01
**Tests:** 187 passed, 0 failed · all changed files lint-clean
**Cost:** every change is $0 (code/config only, no paid-infra spend)

Source of these changes: a 43-agent adversarial audit (28 verified findings).
Full report: `docs/audit-2026-06-01-security-performance.md`. Each fix below was
shipped test-first (TDD).

---

## Performance

- **Per-turn latency is now measurable.** Enabled Pipecat metrics and added a
  `TTFBLogObserver` that logs per-leg TTFB (STT / LLM / TTS) as
  `voice_ttfb call_sid=… processor=… ttfb_ms=…` in the JSON Fly log stream.
  _`voice_pipeline.py`_
- **End-of-call work no longer freezes other calls (P1).** PII redaction
  (Presidio/spaCy) and the SQLite writes ran synchronously in the async
  hang-up path, stalling every concurrent call and new-call setup. They now run
  in a worker thread (`_persist_redacted_transcript_async`). _`voice_pipeline.py`_
- **VAD is actually running again (P4).** In Pipecat 1.2.1 the VAD analyzer is
  read from `LLMUserAggregatorParams`, but the code passed it to the transport
  params where it was silently dropped, so voice-activity detection and
  interruptions weren't engaging. Now wired into the aggregator.
  _`voice_pipeline.py`_, **validate with a live call before prod.**
- Removed a dead `PipelineParams(allow_interruptions=…)` no-op (not a field in
  Pipecat 1.2.1).

## Security

- **Residual PII no longer persisted unredacted (S1).** The audit pass only
  *detected* PII the NER/contextual passes missed; the text written to SQLite
  still contained it (the "quarantine" flag was metadata only). Added
  `redact_pii.mask_residuals()` to scrub flagged spans before any persist/log.
  _`redact_pii.py`, `voice_pipeline.py`_
- **Transcripts no longer streamed to logs (S2).** Dropped the transcript
  `content=…` from the `REDACTED_TRANSCRIPT` log line, it duplicated call
  content (and raw residual PII) into Fly's log retention with no deletion path.
  The SQLite row is the system of record. _`voice_pipeline.py`_
- **Transcript retention + erasure (S3).** The store had no DELETE path, it grew
  forever and couldn't honor erasure, breaking the published 90-day-deletion
  promise. Added `purge_expired` + `delete_by_call_sid` / `delete_by_caller_last4`,
  wired transcript erasure into the DSAR tool, and run a purge at app startup
  (the Fly volume is single-attach, so it must run in the app process).
  _`transcript_store.py`, `data_subject_request.py`, `main.py`_
- **Google Sheets formula injection (S5).** Switched `append_row` / `append_to_tab`
  from `USER_ENTERED` to `RAW` so caller-controlled fields (name, address,
  voicemail transcript) are stored literally, never executed as formulas
  (`=IMPORTRANGE`/`HYPERLINK` data exfiltration). _`sheets_writer.py`_
- **`/auth/demo` brute-force + timing (S7).** Added a rate limit
  (`5/minute;20/hour`) and constant-time credential comparison; the rate limiter
  now keys on the real client IP (`cf-connecting-ip`), not the shared edge.
  _`main.py`_
- **Signed Media-Stream routing (S10).** The Stream routing parameter is now
  HMAC-signed with the client's auth token and verified on the WebSocket, so a
  direct caller can't forge a `To` to start a paid voice session. _`voice_routes.py`_
- **Security response headers (S18):** `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, `Strict-Transport-Security`. _`main.py`_
- **Request body size cap (S15):** reject bodies over 1 MB (configurable via
  `MAX_REQUEST_BYTES`). _`main.py`_
- **Demo-token eviction (S13):** expired tokens are swept when new ones are
  issued, so the in-memory store stays bounded. _`main.py`_
- **Visitor-IP anonymization (S12):** the retained demo-visitor CSV now stores a
  masked IP (IPv4 → /24, IPv6 → /48). _`main.py`_
- **No Host-header trust (S11):** the Media-Stream URL refuses to fall back to the
  attacker-controlled `Host` header in production; `PUBLIC_BASE_URL` is required.
  _`voice_routes.py`_
- **Pinned dependencies (S16):** `requirements.txt` now pins exact, known-good
  versions (the `pipecat-ai` floor was `0.0.50` while `1.2.1` actually runs), so
  builds are reproducible. _`requirements.txt`_

## Scalability

- **Calls no longer dropped on rolling deploys (SC1).** The call hand-off used an
  in-process bridge, so when a 2nd machine briefly exists (every rolling deploy)
  the Media-Stream WebSocket couldn't find the client and the call dropped. The
  inbound number now rides on the Stream as a (signed) parameter, resolving the
  client machine-independently; the bridge remains as a same-machine fallback.
  _`voice_routes.py`_
- **Minute-ceiling admission race (SC5).** Only completed calls were counted, so a
  burst of concurrent calls all passed a stale check and overran the cap.
  Admission now counts in-flight reservations (awaiting-stream + streaming).
  _`voice_routes.py`, `voice_pipeline.py`, `main.py`_
- **`/healthz` check in fly.toml (SC7):** a wedged process is now recycled.
  _`infra/fly/fly.toml`_

## Tests / Tooling

- Fixed a pre-existing cross-file test-isolation bug (`load_dotenv` leaked
  `TWILIO_AUTH_TOKEN` into the voicemail tests), suite went from 8 failing to 0.
- Routing tests now derive client numbers from `config/clients/*.json` instead of
  stale placeholders.

---

## ⚠️ Verify before deploying
1. **VAD (P4)** needs a live staging call, it changes turn-taking/interruptions
   and couldn't be exercised with real audio here.
2. **Redaction model mismatch**, the Dockerfile installs `en_core_web_sm` but the
   redactor expects `en_core_web_lg`; confirm transcripts actually persist in prod.

## Deferred (with rationale in the audit report)
Non-root + multi-stage Docker (S9/S17), default rate limits (S8, would throttle
Twilio's shared IPs), warm-redactor (P3), S6, S14, SC2, SC6, S4, P2, P5.

---

# 2026-06-10, Audit close-out + CI

**Tests:** 264 passed, 0 failed · `ruff check tools/ tests/` clean
**Cost:** every change is $0 (code/config only)

All remaining open audit findings are now closed or deliberately deferred.
GDPR-specific work is out of scope (product decision 2026-06-10); the
gdpr_prefix code paths remain in place but untouched.

## Shipped (TDD)
- **P2**, deleted the dead Smart Turn warm-up (never used by any pipeline):
  ~4s faster startup, ONNX graph no longer pinned in RAM on the 1 GB box.
- **P3**, PII redactor (Presidio + spaCy) warmed at startup; the first
  hangup after a restart no longer pays a multi-second model load.
- **P5**, system prompt is now byte-stable across calls (cache READS, not a
  write per call); the time line rides as a separate message and is
  hour-granular so calls in the same hour share an Anthropic cache prefix.
  Applied to voice and text-chat paths. (~$9–10/mo at 1k calls/mo, plus
  first-turn latency.)
- **S13**, `/auth/logout` revokes demo bearer tokens (sweep already shipped).
- **S15 (completion)**, chunked/streamed bodies with no Content-Length are
  now counted and rejected at MAX_REQUEST_BYTES with 413.
- **SC6**, per-thread SQLite connections; the global read/write lock is gone
  (WAL arbitrates), so simultaneous hang-ups no longer queue behind one lock.
- **S17**, multi-stage Docker image; no build toolchain in the runtime stage.
- **S16 (completion)**, `requirements.lock` (uv, --generate-hashes, 144
  packages); image + CI install from it.
- **CI**, `.github/workflows/ci.yml` runs ruff + pytest on every push/PR
  (fires on next push; branch is intentionally unpushed).
- **ruff baseline**, 25 pre-existing violations fixed; lint is now clean.

## Verified, no change needed
- **SC1**, already fixed in code: signed `to`/`sig`/`exp` TwiML Stream
  params are the primary, machine-independent client resolution;
  `pending_calls` survives only as a same-machine deploy-window fallback.
  The audit table's empty status was stale.

## Deferred (rationale in the audit report)
- **SC2**, stateless demo tokens: demo-path only, single-machine deploy;
  revisit when a 2nd machine ships.
- **S14 remainder**, explicit tighter /vm rate limit: global 240/min/IP
  default already throttles; not worth refactoring the limiter out of
  `_build_app` now.
- **S10 remainder**, per-call nonce: the signed routing token covers the
  realistic threat.

## ⚠️ Verify before deploying
- Multi-stage Dockerfile + hash lockfile are statically tested only (no
  container runtime on the dev box). Do a **staging `fly deploy`** first:
  wheels for all deps must build in the builder stage and the app user must
  still write /data/transcripts.db.
- P5 changes the prompt-cache layout: after a staging call, check the logs'
  usage metrics show `cache_read_tokens` > 0 on the second call within the
  same hour.
