# Phase 1: Pilot Ready - Context

**Gathered:** 2026-05-07
**Status:** Ready for planning

<domain>
## Phase Boundary

Add All Green Pest Control as a second isolated client alongside Clear Slope. All Green gets its own Twilio phone number, branding config, Google Sheet for bookings, and a hard monthly minute ceiling, all without touching Clear Slope's data or config. Twilio account is upgraded off trial and All Green's number is provisioned and routed to the app.

</domain>

<decisions>
## Implementation Decisions

### Client Routing & Registry
- **D-01:** Inbound Twilio `To` number is the routing key, `POST /twilio/voice` maps `To` → client config + writer + notifier.
- **D-02:** Client registry is derived at startup by scanning `config/clients/*.json`. No separate registry file. Adding a new client = drop a JSON file, set env vars, restart.
- **D-03:** The registry is a `dict[to_number, ClientContext]` built in `main.py` at startup (per the existing `TODO(multi-client)` comment in `sheets_writer.py`).

### Secrets Per Client
- **D-04:** Prefixed env vars per client, `CLEARSLOPE_*` and `ALLGREEN_*`. Each client JSON specifies its env prefix.
- **D-05:** Shared Google service account (`GOOGLE_SERVICE_ACCOUNT_FILE` stays a single global var). Only `{PREFIX}_GOOGLE_SHEET_ID` differs per client.
- **D-06:** Per-client vars needed: `{PREFIX}_GOOGLE_SHEET_ID`, `{PREFIX}_TWILIO_SMS_FROM`, `{PREFIX}_OWNER_NOTIFY_NUMBER`. `TWILIO_AUTH_TOKEN` stays global (shared account, one token) for now.

### Minute Ceiling Tracking
- **D-07:** `minute_ceiling` field added to client JSON config (`config/clients/allgreen.json`). `null` or absent = no ceiling (Clear Slope unchanged).
- **D-08:** In-memory counter per client, resets on restart. Acceptable trade-off for a 30-day pilot, a restart gives a brief reprieve, not a meaningful cost risk.
- **D-09:** Enforcement at call start only, check counter before starting the Pipecat pipeline. If ceiling is hit: play a brief TTS message ("Our answering service is temporarily unavailable, please call back during business hours") and hang up gracefully. No mid-call interruption.
- **D-10:** Counter increments at call end, wall-clock seconds from pipeline start to pipeline end, converted to minutes (round up), added to the client's monthly total. Logged as structured JSON.

### Twilio Account Structure
- **D-11:** Shared Twilio account, second phone number for All Green. One `TWILIO_AUTH_TOKEN` for both clients.
- **D-12:** Signature validator (`_validate_twilio_signature`) is made client-aware, auth token flows through the client registry (currently same value for both). This means zero refactor when subaccounts are added later.
- **D-13:** All Green's number is provisioned manually in the Twilio console; webhook pointed at the existing app URL. TWIL-01 (trial upgrade) also done manually.

### Deployment Topology
- **D-14:** Same Fly.io app (`clearslope-receptionist`) serves both clients. Twilio webhook URLs for both numbers point at the same host.
- **D-15:** No app rename. No CORS changes (voice is server-to-server; All Green has no browser frontend in Phase 1).

### Future Notes (not in scope for Phase 1)
- Twilio subaccounts (per-client SID + auth token) are the upgrade path for multi-client scale.
- Twilio number provisioning can be automated via the Twilio API when onboarding more clients.
- Minute ceiling persistence (file-backed) can be added if restart-reset becomes a concern.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Requirements
- `.planning/REQUIREMENTS.md`, All 10 Phase 1 requirements (TENT-01–05, COST-01–03, TWIL-01–02) with acceptance criteria
- `.planning/ROADMAP.md`, Phase 1 success criteria (the 5 testable outcomes)
- `.planning/PROJECT.md`, Key decisions log, constraints, scope boundaries

### Existing Code (must read before modifying)
- `tools/llm_proxy/client_config.py`, Existing `ClientConfig` dataclass and JSON loader; Phase 1 adds `minute_ceiling` field and `twilio_number` field
- `tools/llm_proxy/main.py`, App startup; Phase 1 replaces single-client init with multi-client registry
- `tools/llm_proxy/voice_routes.py`, Twilio webhook handlers; Phase 1 adds `To`-based client lookup and ceiling check
- `tools/llm_proxy/sheets_writer.py`, `writer_from_env()` has `TODO(multi-client)` comment pointing at the dict-based design
- `tools/llm_proxy/voice_pipeline.py`, `run_voice_session()` signature may need client context passed through

### Config
- `config/clients/clearslope.json`, Canonical example of the client JSON schema; `allgreen.json` must follow same shape + add `minute_ceiling`

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ClientConfig` dataclass (`client_config.py`): Already handles business identity, hours, timezone, prompt paths. Phase 1 extends it with `minute_ceiling: int | None` and `twilio_number: str` fields.
- `SheetsWriter` + `writer_from_env()` (`sheets_writer.py`): Existing writer works per-client already, just needs to be instantiated once per client at startup instead of once globally.
- `SmsNotifier` + `notifier_from_env()` (`sms_notifier.py`): Same pattern, instantiate per-client with the client's SMS vars.
- `_validate_twilio_signature()` (`voice_routes.py`): Needs the auth token passed in (currently reads from env directly); thread it through the client context.

### Established Patterns
- Per-client config via `config/clients/*.json`, already in use for Clear Slope. All Green follows the same pattern.
- `app.state.*` for shared resources, current pattern stores single writer/notifier/prompt on app state. Phase 1 replaces these with a `dict[to_number, ClientContext]` on `app.state.clients`.
- Structured JSON logging, already in place. Minute usage logs follow the same format.

### Integration Points
- `POST /twilio/voice`: Must extract `To` from form data, look up client in registry, pass client context through to pipeline.
- `WS /twilio/stream`: Receives `call_sid` and `stream_sid`; client context needs to be threaded from the `/voice` handler (via a short-lived in-memory map keyed by `call_sid`) or re-derived from the stream's custom parameters.
- `run_voice_session()`: Needs `client_context` parameter so it can check/update the minute counter, use the right SheetsWriter, and pass the right auth token to the validator.

</code_context>

<specifics>
## Specific Ideas

- The `TODO(multi-client)` comment in `sheets_writer.py` already says: "Replace with a `dict[twilio_to_number, SheetsWriter]` keyed by the inbound Twilio number, constructed at app startup." Follow this design exactly.
- Graceful ceiling rejection message: "Our answering service is temporarily unavailable, please call back during business hours." (TTS via Cartesia, then hang up.)
- `minute_ceiling: 200` is the All Green pilot value (covers ~33–46 after-hours calls/mo with headroom).

</specifics>

<deferred>
## Deferred Ideas

- **Twilio subaccounts**, per-client SID + auth token for full billing isolation. Upgrade path when scaling beyond 2–3 clients. (Phase 3 or later)
- **Automated Twilio number provisioning**, script to purchase + configure numbers via Twilio API. (Future onboarding tooling)
- **Persistent minute counter**, file-backed counter that survives restarts. Add if restart-reset becomes a concern during the pilot. (Phase 1 follow-up or v2)
- **All Green demo UI / CORS**, if a browser-based demo is built for All Green, add its origin to CORS allowlist. (Phase 2 or later)

</deferred>

---

*Phase: 1-Pilot Ready*
*Context gathered: 2026-05-07*
