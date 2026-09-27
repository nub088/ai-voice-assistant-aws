# Voicemail Fallback, Design Spec

**Date:** 2026-05-21
**Status:** Draft, pending user review
**Scope:** v1 of "caller doesn't want to talk to the AI receptionist" path

## Problem

Today, when a caller dials a client's Twilio number, Sarah (the AI receptionist) tries to take their info regardless of whether they want to talk to a bot. Some callers explicitly refuse, "just take a message", "no AI", "I want a human", and the current behaviour creates a poor experience and risks losing the lead.

We need a graceful fallback that:
- Detects when a caller doesn't want to speak to the AI.
- Records a voicemail on Twilio (no third-party storage).
- Texts the business owner an alert with caller info, a transcript, and a tappable playback link.
- Automatically purges the audio after 30 days and the local metadata after 90 days, consistent with the existing privacy posture.

## Locked Decisions

| Decision | Choice |
|---|---|
| Trigger detection | AI-driven via new `transfer_to_voicemail` function tool. No DTMF menu. |
| SMS payload | Rich: caller# (redacted), duration, transcript snippet, playback URL. |
| Playback URL hosting | Proxied through our service with HMAC-signed token. Twilio recording stays private. |
| Audio retention | 30 days on Twilio. Daily Fly scheduled job deletes via Twilio REST. |
| Metadata retention | 90 days. Stored in a new "Voicemails" tab in the existing client Google Sheet. Same job prunes rows. |
| Recording duration cap | 120 seconds (matches Twilio built-in transcription's 2-minute limit). |
| Transcription | Twilio built-in (free). Fall back to "(no transcript available)" if it fails or returns empty. |

## Architecture, Call Flow

```
1. Caller dials Twilio number.
2. /twilio/voice returns TwiML <Connect><Stream> (unchanged).
3. Pipecat session runs.
4. Caller signals refusal ("just take a message", "no AI", repeat frustration).
5. Sarah invokes transfer_to_voicemail(reason=<short>).
6. Handler:
     a. Twilio REST: calls(call_sid).update(url=<our_app>/twilio/voicemail.xml)
        - Twilio drops the live call into the new TwiML.
     b. End Pipecat task cleanly so the Stream WS closes.
7. /twilio/voicemail.xml returns:
     <Say>Sorry about that. Leave your message after the beep.</Say>
     <Record  maxLength=120  playBeep=true
              transcribe=true
              transcribeCallback=/twilio/voicemail/transcription
              recordingStatusCallback=/twilio/voicemail/recording
              finishOnKey="#"/>
     <Say>Thanks, we got your message.</Say>
     <Hangup/>
8. Twilio POSTs recording metadata → /twilio/voicemail/recording.
9. Twilio POSTs transcription text → /twilio/voicemail/transcription.
10. Once both arrive (or 60s timer fires): SMS the owner.
```

The `calls.update` swap is seamless to the caller, no dead-air.

## Components

### New Files

- **`tools/llm_proxy/voicemail.py`**, single module holding:
  - `transfer_to_voicemail` function-tool schema + handler factory (mirrors the `function_tools.py` pattern).
  - `/twilio/voicemail.xml` route (returns the `<Record>` TwiML).
  - `/twilio/voicemail/recording` callback (persists a row in the Voicemails sheet).
  - `/twilio/voicemail/transcription` callback (updates the row, fires SMS).
  - `/vm/{recording_sid}` proxy route (validates HMAC token, streams MP3 from Twilio).

- **`tools/voicemail_retention.py`**, Fly scheduled job, same shape as `retention_enforcer.py`. Two passes:
  - Pass 1: Twilio API `DELETE` on recordings > 30 days old (scoped per registered client).
  - Pass 2: Sheet rows > 90 days old.

### Modified Files

- **`tools/llm_proxy/function_tools.py`**, register the new tool alongside `log_request` / `end_call`.
- **`tools/llm_proxy/voice_routes.py`**, mount the new router (or wire it via `main.py`, whichever matches current pattern; confirm during plan phase).
- **`tools/llm_proxy/sms_notifier.py`**, add `send_voicemail_alert(...)` method. Reuses the existing Twilio client, `owner_number`, and `_send` failure handling.
- **`tools/prompts/receptionist_v0.3_data_driven.md`**, add a short block: "if the caller declines to speak with you or asks for voicemail, call `transfer_to_voicemail`; do not push back."
- **`config/clients/*.json`**, no schema change. Reuses existing `{PREFIX}_OWNER_NOTIFY_NUMBER` env var.

### Voicemail Metadata Storage

New "Voicemails" tab in the per-client Google Sheet, written via the existing `SheetsWriter`. Columns:

| Column | Source |
|---|---|
| `created_at` (ISO-8601 UTC) | `recording-status` callback time |
| `call_sid` | Twilio |
| `recording_sid` | Twilio |
| `caller_number_last4` | redacted, matches existing PIPA convention |
| `duration_seconds` | Twilio |
| `transcript` | Twilio (or empty) |
| `playback_url_token_expires_at` | for reference; the URL itself is constructed on-demand from the SID |
| `sms_sent_at` | filled after SMS dispatch |

## SMS Format

```
New voicemail, All Green
From: +1604***1234  (0:42)
"Hi this is Bob, my house has rats by the
 garage, call me back at 555-0100."
Listen: https://<app>.fly.dev/vm/RE<sid>?t=<token>
Expires in 30 days.
```

- Transcript truncated to ~300 chars in the SMS; full transcript lives in the sheet row.
- If transcription is empty/missing: replace with `(no transcript available)`.
- Anonymous callers: `From: <anonymous>`. Voicemail still records.

## Signed Playback URL, `/vm/{recording_sid}?t=<token>`

**Token construction:**
- Query string carries two values: `?exp=<expires_epoch>&sig=<signature>`.
- `signature = base64url( HMAC-SHA256(secret, f"{recording_sid}:{expires_epoch}") )`, full 32-byte digest, ~43 chars base64url, no truncation.
- Stateless, verification recomputes the HMAC over the recording_sid (from the path) and exp (from the query) using the secret; no DB lookup.
- `secret` = new env var `VOICEMAIL_URL_SECRET` (random 32-byte, per-deployment). Service fails fast on startup if missing.
- `expires_epoch` = `created_at + 30 days`, matching the audio retention window.

**Route handler:**
1. Parse token → verify HMAC + expiry.
2. On invalid/expired: 403.
3. Stream the MP3 from `https://api.twilio.com/2010-04-01/Accounts/{SID}/Recordings/{SID}.mp3` using basic auth, pass-through `Content-Type: audio/mpeg`.
4. On Twilio 404 (audio deleted by retention job): return 410 HTML "This voicemail has expired."
5. No caching, no Range support in v1.

**Trade-off accepted:** Anyone with the URL can play the recording during the 30-day window. The SMS itself already grants access to whoever has the owner's phone, so this is no worse than the SMS delivery channel.

## SMS Timing

Twilio fires `recording-status` quickly and `transcription` later (seconds to minutes; can fail entirely ~30% of the time). Logic:

- Wait for transcription callback OR 60s timer, whichever first.
- If timer wins: SMS goes out with `(transcript pending)`. If the transcription arrives later, the sheet row is updated; the SMS is NOT re-sent.

## Retention Job, `tools/voicemail_retention.py`

Mirrors `retention_enforcer.py`: `--dry-run` is default, `--apply` actually deletes, JSON-per-action logging.

```
Pass 1, Twilio recordings older than 30 days
  for client in registered clients:
      account_sid, auth_token = client creds
      cutoff = today - 30d
      for recording in twilio.recordings.list(date_created_before=cutoff):
          if --apply: twilio.recordings(recording.sid).delete()
          log {"action": "delete_recording", "sid": ..., "age_days": ..., "client_id": ...}

Pass 2, Sheet rows older than 90 days
  for client in registered clients:
      sheet = "Voicemails" tab
      for row in sheet.values:
          if row.created_at < today - 90d:
              if --apply: delete the row
              log {"action": "delete_row", "row_id": ..., "age_days": ..., "client_id": ...}
```

**Scoping:** the Twilio account is shared across clients (D-11). The job filters recordings by call ownership (CallSid → inbound `To` number → client) so deletion for client A never touches client B's recordings.

**Cost:** Twilio recording storage is ~$0.0025/min/month. A 2-min voicemail held 30 days is ~$0.0001. Delete API calls are free. The job runs on the existing Fly scheduled-machine pattern, no extra compute.

**Idempotency:** Re-running deletes nothing already deleted. 404 from Twilio is treated as already-gone.

**Not deleted by this job:**
- Twilio SMS log of the alert SMS (owner's phone, Twilio's SMS log).
- Twilio call log / billing record (Twilio's retention).
- Transcription resource, deleted automatically when the parent Recording is deleted.

## Error Handling

| Failure | Behaviour |
|---|---|
| `transfer_to_voicemail` tool fires but Twilio `calls.update` returns 4xx/5xx | Log failure; fall back to existing `end_call` with a polite "leave us a message at our other line" message. |
| Pipecat task doesn't end cleanly after `calls.update` | Stream WS is killed by Twilio when the call moves; treat disconnect as expected. |
| `/twilio/voicemail/recording` callback never arrives | No SMS, no row. Caller still heard "we got your message". Rare; tolerated. |
| `/twilio/voicemail/transcription` callback never arrives | 60s timer fires; SMS sent with `(transcript pending)`. If callback arrives later, sheet updated; SMS not re-sent. |
| SMS to owner fails | Existing `SmsNotifier._send` logs and swallows. Row still in sheet so owner sees it on next sheet check. |
| Owner taps URL after audio deleted | 410 HTML: "This voicemail has expired." |
| Owner shares the URL | Anyone with URL can play during the 30-day window. Accepted trade-off (see Signed URL section). |
| `VOICEMAIL_URL_SECRET` missing | Service refuses to start (fail-fast, matches existing `auth_token` check). |
| Retention job partial failure | Idempotent; next day picks up. JSON log identifies failed SIDs. |
| Anonymous caller | Voicemail records normally. SMS shows `From: <anonymous>`. No caller-confirmation SMS (matches existing `_ANONYMOUS_FROM_VALUES`). |
| Caller over the per-client minute ceiling | The original `/twilio/voice` ceiling check already short-circuits before this path; no change needed. |

## Testing

### Unit Tests (new under `tests/`)

- **`test_voicemail_tool.py`**, `transfer_to_voicemail` handler calls Twilio `calls.update` with the right URL, ends the pipeline on success, falls back on failure. Uses the fake-Twilio pattern from `test_sms_notifier.py`.
- **`test_voicemail_routes.py`**, `/twilio/voicemail.xml` returns valid TwiML; `/twilio/voicemail/recording` writes a sheet row; `/twilio/voicemail/transcription` updates the row and fires SMS; signature validation rejects unsigned requests.
- **`test_signed_url.py`**, token roundtrip (sign → verify), expired tokens rejected, tampered tokens rejected.
- **`test_voicemail_retention.py`**, dry-run lists nothing for deletion; `--apply` calls `recordings.delete` exactly for SIDs past cutoff; treats 404s as already-deleted; client-scoping correct.

### No Live Twilio in CI

All tests use the structural-typing fake-client pattern already established in `sms_notifier.py`.

### Manual Test Plan (documented in `RUNBOOK.md`)

1. Stage with a `live: false` client. Call in. Say "I just want to leave a message."
2. Confirm seamless handoff, beep, record 10s, hear "Thanks", call ends.
3. Confirm SMS arrives at owner number within ~90s.
4. Tap SMS URL on a phone with no Twilio creds, confirm playback.
5. Wait 30 days OR force-run retention job with manipulated cutoff, confirm recording 404s and URL returns 410.

## Out of Scope (v1)

Naming these explicitly so we don't quietly add them:

- Custom per-client voicemail greeting (hardcoded `<Say>` for now).
- DTMF "press 1 for voicemail" upfront.
- Voicemail-to-email.
- Multi-recipient owner notifications.
- Re-sending SMS if it fails (one shot only).
- Per-recipient URL tokens (URL is shareable within the 30-day window).
- Range/streaming optimization on `/vm/{sid}` playback (full MP3 served).

## New Environment Variables

| Var | Scope | Purpose |
|---|---|---|
| `VOICEMAIL_URL_SECRET` | global | HMAC secret for signing playback URLs. Service fails fast if missing. |

All other inputs (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `{PREFIX}_OWNER_NOTIFY_NUMBER`, `{PREFIX}_TWILIO_SMS_FROM`) are existing.

## Open Questions

None, all four trigger/payload/hosting/retention decisions are locked above. Implementation-mechanics details (router wiring location, exact sheet writer shape) will be confirmed during the plan phase by reading the current code.
