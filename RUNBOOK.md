# Operations Runbook

What to do when something is wrong. Bias toward "look at logs first, then check the upstream dashboard."

---

## Multi-Tenant Operations

The app now serves multiple clients from one Fly.io machine. Each client has:
- A JSON file in `config/clients/<client_id>.json` (twilio_number, env_prefix, minute_ceiling)
- Prefixed env secrets in Fly: `{PREFIX}_GOOGLE_SHEET_ID`, `{PREFIX}_TWILIO_SMS_FROM`, `{PREFIX}_OWNER_NOTIFY_NUMBER`
- An in-memory minute counter reset on each deploy

### Onboard a 3rd client

1. Copy `config/clients/clearslope.json` → `config/clients/<new_client>.json`
2. Fill in `client_id`, `twilio_number` (E.164), `env_prefix` (e.g. `ACME`), `minute_ceiling` (or `null`)
3. Fill in business identity (name, short_name, industry, ai_name) and hours
4. Set prefixed secrets:
   ```bash
   fly secrets set --app clearslope-receptionist \
     ACME_GOOGLE_SHEET_ID=<sheet-id> \
     ACME_TWILIO_SMS_FROM=<twilio-number> \
     ACME_OWNER_NOTIFY_NUMBER=<owner-cell>
   ```
5. In Twilio console: provision a number → webhook → `https://clearslope-receptionist.fly.dev/twilio/voice`
6. Commit the JSON and deploy:
   ```bash
   git add config/clients/<new_client>.json && git commit -m "ops: add <new_client> client"
   # Run from project root, the Dockerfile expects the repo root as build
   # context (COPY tools, COPY config). Running from infra/fly/ will fail.
   flyctl deploy -c infra/fly/fly.toml --app clearslope-receptionist
   ```
7. Verify: `curl .../healthz | jq '.clients'` should list the new number

### Read per-client minute usage from logs

```bash
# Cumulative usage for All Green since last deploy
fly logs --app clearslope-receptionist | grep "minutes_charged" | grep "client_id=allgreen"

# Parse total_minutes from each call (ascending):
fly logs | grep "client_id=allgreen" | grep -oP 'total_minutes=\K[0-9.]+'
```

Each call logs: `call_sid=... client_id=... elapsed_sec=... minutes_charged=... total_minutes=...`

### Reset minute counters

Counters are in-memory and reset automatically on every deploy or machine restart:

```bash
fly machine restart <machine-id> --app clearslope-receptionist
```

For a scheduled reset (e.g. monthly): deploy the app at the start of each billing cycle.

### Check per-client Sheet readiness

```bash
curl https://clearslope-receptionist.fly.dev/healthz | jq '{clients, clients_with_no_sheet}'
# clients_with_no_sheet: [] means all clients have sheets configured
```

If a client appears in `clients_with_no_sheet`, set `{PREFIX}_GOOGLE_SHEET_ID` and redeploy.

---

## Quick reference

```
fly logs -a clearslope-receptionist          # live tail
fly status -a clearslope-receptionist        # machines + version
fly machine restart <machine-id> -a clearslope-receptionist
fly secrets list -a clearslope-receptionist  # names only, no values
curl https://clearslope-receptionist.fly.dev/healthz   # 200 ok / 503 degraded
```

The log format is JSON, one object per line. Every log line has `ts`, `level`, `logger`, `msg`. Lines from a single call carry `call_sid` (extracted from the message body). Filter with `jq`:

```
fly logs -a clearslope-receptionist | jq -r 'select(.call_sid == "CA<sid>") | .msg'
```

---

## Symptom: calls aren't connecting

### Step 1: confirm the deploy is healthy

```
curl https://clearslope-receptionist.fly.dev/healthz
```

- **200 with `"status": "ok"`** → app is up; the issue is upstream. Skip to step 2.
- **503 with `"missing": [...]`** → a required secret is missing. The named keys map to: `anthropic` (ANTHROPIC_API_KEY), `deepgram` (DEEPGRAM_API_KEY), `cartesia` (CARTESIA_API_KEY + CARTESIA_VOICE_ID), `twilio_signature` (TWILIO_AUTH_TOKEN), `sheets_writer` (at least one client has no `{PREFIX}_GOOGLE_SHEET_ID`; check `clients_with_no_sheet` in the response). Re-set the missing secret, deploy will auto-redeploy.
- **Connection refused / timeout** → app is down. `fly status -a clearslope-receptionist`. Restart the machine if needed.

### Step 2: check Twilio's view

Twilio Console → Phone Numbers → the receptionist number → Voice & Fax tab.

The "A Call Comes In" webhook should be `https://clearslope-receptionist.fly.dev/twilio/voice` (HTTP POST). If it's pointing somewhere else, or the URL is missing, that's why calls aren't connecting.

Twilio Console → Monitor → Logs → Calls. Find a recent call attempt. The error code tells you what failed:
- **11200 / HTTP retrieval failure**, our app didn't respond. Check fly logs for that timeframe.
- **31920 / Stream connection error**, the Media Stream WebSocket wasn't reachable. Confirm `PUBLIC_BASE_URL` in fly secrets matches the actual host.
- **32011 / TwiML signature**, TWILIO_AUTH_TOKEN in fly secrets doesn't match the Twilio account. Rotate.

### Step 3: check fly logs

```
fly logs -a clearslope-receptionist | grep -i error | tail
```

Common patterns:
- **"server misconfigured: signature verification unavailable"**, TWILIO_AUTH_TOKEN secret got cleared. Re-set it.
- **"twilio signature validation failed for url=..."**, production token got rotated in Twilio but not here. `fly secrets set TWILIO_AUTH_TOKEN=<new>`.
- **Pipecat / Deepgram WebSocket disconnect**, usually transient. If persistent, check Deepgram status page.

---

## Symptom: caller says the AI sounds wrong / weird / cut off

Most likely a Cartesia (TTS) issue. Audio glitches, robotic voice, or long silences usually trace to TTS, not the LLM.

```
fly logs -a clearslope-receptionist | jq -r 'select(.logger | test("cartesia"; "i")) | "\(.ts) \(.level) \(.msg)"' | tail -20
```

- **"Cartesia TTS failed"** in logs → check status.cartesia.ai. If they're degraded, calls in flight will continue with whatever audio Cartesia returns; nothing to do but wait.
- **CARTESIA_VOICE_ID changed** → the Sarah voice model may have been replaced. Verify the voice ID in fly secrets matches a current voice in the Cartesia dashboard.
- **Latency** → `shared-cpu-1x` machine under load. If happening only during multiple concurrent calls, see "Capacity issues" below.

For LLM weirdness (off-topic answers, made-up services, broken tone):
1. Check whether anyone changed `tools/prompts/receptionist_v0.3_data_driven.md` recently (`git log -- tools/prompts/`).
2. Reproduce with `python tools/replay_transcript.py tests/replay_scenarios/<scenario>.json` before touching the prompt.

---

## Symptom: a booking is missing from the Sheet

Customer says they called and gave details but no row landed in the booking sheet.

Search logs for the dropped-payload marker:

```
fly logs -a clearslope-receptionist | grep -i "log_request DROPPED" | tail
```

Each dropped row is logged at ERROR with the full tool-call payload as `row=<json>`. To recover:
1. Pull the JSON out of the log line.
2. Paste the relevant fields into the booking sheet manually.
3. Investigate why the Sheets append failed, usually one of:
   - **Quota exceeded**, Google Sheets API quota reset is daily; wait or increase quota.
   - **Service account permission revoked**, the service account email needs Editor on the sheet. Check sheet sharing.
   - **Sheet renamed/deleted**, `GOOGLE_SHEET_ID` secret no longer points at a valid sheet.

If no `DROPPED` line exists, the call didn't reach the `log_request` tool, the AI may have hung up before saving. Pull the full transcript with `call_sid=` filter and look at how the call ended.

---

## Symptom: SMS isn't arriving

### Customer confirmation

```
fly logs -a clearslope-receptionist | grep "sms customer_confirmation"
```

- "skipped: TWILIO_SMS_FROM not configured" → set the secret.
- "failed: status=21610" → recipient has STOP-replied. Permanent block.
- "failed: status=21408" → number couldn't receive SMS (landline, blocked, etc.). Not actionable.
- No log line at all → the call ended without invoking `log_request`. See "booking is missing" above.

### Owner emergency alert

```
fly logs -a clearslope-receptionist | grep "sms owner_emergency"
```

If `OWNER_NOTIFY_NUMBER` is empty, the alert is silently skipped, `fly secrets list` to confirm it's set.

---

## Symptom: app is down (machine crashed)

```
fly status -a clearslope-receptionist
```

If a machine shows `stopped`/`failed`:

```
fly machine restart <machine-id> -a clearslope-receptionist
```

If multiple restarts fail, check recent commits for an introduced startup error:

```
fly logs -a clearslope-receptionist | jq -r 'select(.level=="ERROR" or .level=="CRITICAL") | "\(.ts) \(.msg)"' | head
```

Last-resort: roll back to the previous good release.

```
fly releases -a clearslope-receptionist
fly deploy -a clearslope-receptionist --image registry.fly.io/clearslope-receptionist:<previous-tag>
```

---

## Symptom: cost spike / quota exhausted

If Anthropic, Deepgram, or Cartesia bills jump unexpectedly:

1. **Check call volume in Twilio** (Monitor → Logs → Calls). If volume is normal, the issue is per-call cost.
2. **Pull recent calls' duration**: long calls (>10 min) may indicate a runaway loop where the AI never calls `end_call`.
3. **Check the `end_call` log lines**:

   ```
   fly logs -a clearslope-receptionist | grep "end_call invoked" | tail
   ```

   If far fewer `end_call invoked` lines than calls reached the LLM, the prompt may have regressed on closing behavior.

**Hard kill if needed**: rotate the Anthropic key in fly secrets to something invalid → app fails fast on each call → no further LLM spend until you re-enable.

---

## Cost monitoring (one-time setup)

Set spend alerts in each upstream's dashboard so a runaway loop or volume
spike doesn't burn an entire month's budget unannounced.

### Anthropic
1. console.anthropic.com → Billing → Usage limits.
2. Set a **monthly hard cap** (e.g., 3× the current month's spend). Hard caps
   reject API calls past the threshold, preferable to a soft alert because
   it stops the bleeding without anyone needing to react.
3. Set a **soft alert** at 50% and 80% to email the owner.

### Deepgram
1. console.deepgram.com → Billing.
2. Set a usage alert at the spend threshold equivalent to ~5x normal monthly
   STT minutes.
3. There is no hard cap, only alerts. Treat the alert as a paging signal.

### Cartesia
1. play.cartesia.ai → Settings → Billing.
2. Same pattern: alert at 50% and 80% of the expected monthly TTS spend.

### Twilio
1. Twilio Console → Account → Usage Triggers.
2. Create a trigger on **Total Price** monthly with a threshold of ~$200
   (adjust to your actual baseline). Notification: email + webhook to the
   owner.
3. Twilio supports per-feature triggers (Voice minutes, SMS count), useful
   if you want to catch SMS-bombing attacks separately from call-volume
   attacks.

### Fly.io
1. fly.io/dashboard/<org>/billing.
2. Fly does not support hard caps; set a **soft alert** at the current
   monthly spend × 2 so you notice if a runaway worker is getting
   provisioned.

When an alert fires, the sequence in "Cost spike / quota exhausted" above
is the right starting playbook.

---

## Voicemail fallback

### Required env vars
- `VOICEMAIL_URL_SECRET`, 32-byte random secret. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `PUBLIC_BASE_URL`, already required by the voice path.
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, already required.

### Manual smoke test

1. Deploy with `VOICEMAIL_URL_SECRET` set. Confirm `/healthz` reports `voicemail_secret: true`.
2. Call the staging Twilio number. When Sarah greets, say "I just want to leave a voicemail."
3. Confirm the call seamlessly hands off to the beep within ~1 second.
4. Leave a short test message (≤10s). Press `#` to finish or wait for the 120s cap.
5. Hear "Thanks, we got your message." and the call ends.
6. Within ~90 seconds the owner phone (`{PREFIX}_OWNER_NOTIFY_NUMBER`) should receive an SMS containing:
   - "New voicemail, {short_name}"
   - Caller number redacted to last 4
   - Duration in MM:SS
   - Transcript (or "(transcript pending)" / "(no transcript available)")
   - A `https://<app>.fly.dev/vm/RE...` URL
7. Tap the URL on a device with no Twilio creds. Confirm the MP3 plays.
8. Check the client's Google Sheet, a row should appear in the "Voicemails" tab with all fields populated.

### Retention test

```bash
# Dry-run (default)
python -m tools.voicemail_retention --prefix ALLGREEN
```

**Defaults**: `--audio-days` is now `1` (was 30) and `--metadata-days`
is `90`. The Fly retention machine runs daily, so Twilio recordings get
pruned 24-48 hours after creation. Sheet rows in the Voicemails tab are
pruned at 90 days.

```bash
# Apply for real
python -m tools.voicemail_retention --apply --prefix ALLGREEN

# Force-prune for testing (1-day audio, 1-day metadata)
python -m tools.voicemail_retention --apply --audio-days 1 --metadata-days 1 --prefix ALLGREEN
```

After running with `--apply --audio-days 1`, tapping an old SMS URL should return a 410 "This voicemail has expired." page.

The retention job exits 0 on success, 1 if either pass reported errors, use that to alert on the Fly scheduled run.

### Scheduling on Fly

Both retention jobs (`retention_enforcer.py` and `voicemail_retention.py`) are run daily by a Fly scheduled machine defined in `infra/fly/fly.toml` under `[processes].retention`. The machine boots, runs `tools/run_daily_retention.py` with `--apply`, and stops. Expected cost: well under $0.05/month on shared-cpu-1x@256MB.

To inspect the next/last run:
```bash
fly machine list --app clearslope-receptionist | grep retention
fly logs --app clearslope-receptionist --machine <id>
```

To trigger an ad-hoc run without waiting for the schedule:
```bash
fly machine run --app clearslope-receptionist <image> --process retention
```

The `tools.run_daily_retention` script exits non-zero if any pass reports errors, so failed runs are visible in `fly logs`.

**Schedule timing.** Fly's `schedule = "daily"` runs at ~00:00 UTC; there is no time-of-day knob in `fly.toml`. For US Pacific clients this is ~16:00–17:00 local. With `--audio-days=1` as the default, the daily cron yields a practical 24-48h retention window for voicemail audio. Retention only touches recordings created before the cutoff, so the timing has no impact on live traffic.

**Apply safety.** The runner refuses to delete without `RETENTION_ALLOW_APPLY=1` in the environment, `infra/fly/entrypoint.sh` sets this in the `retention` process. A developer running `python -m tools.run_daily_retention` locally with production creds in `.env` exits `2` without touching data. (For manual one-off testing, use `python -m tools.voicemail_retention --apply --prefix <PREFIX>` instead, that path is unguarded by design.)

**Per-client behavior.** The runner globs `config/clients/*.json` and runs the sheet-row prune once per client (`env_prefix`). The Twilio audio prune runs once account-wide. Adding a new client config is picked up on the next run with no code change.

---

## Transcript store

Redacted call transcripts are persisted to SQLite at `/data/transcripts.db`
on a Fly volume mounted on the receptionist app machine. This is in
addition to the `REDACTED_TRANSCRIPT` log line (which remains as a
safety-net surface during rollout).

### One-time volume create

Before the first deploy that includes the new `[[mounts]]` block in
`infra/fly/fly.toml`, create the volume:

```bash
fly volumes create transcripts_data --size 1 --region sjc \
  --app clearslope-receptionist
```

Skipping this step makes the deploy fail with "volume not found." Doing
it twice fails with "volume already exists", safe to ignore.

### Inspecting transcripts

The DB is a regular SQLite file. SSH into the machine and use the
standard CLI:

```bash
fly ssh console --app clearslope-receptionist
# inside the machine:
sqlite3 /data/transcripts.db
sqlite> SELECT call_sid, client_id, created_at FROM transcripts
        ORDER BY created_at DESC LIMIT 20;
```

For ad-hoc analysis from a laptop, copy the file off:

```bash
fly ssh sftp shell --app clearslope-receptionist
sftp> get /data/transcripts.db /tmp/transcripts.db
sftp> exit
sqlite3 /tmp/transcripts.db
```

### When SQLite isn't enough

The DB is single-machine: if we ever auto-scale or run multiple
receptionist machines, the volume becomes a single point of bottleneck
and SQLite serializes writes through one process. At that point migrate
to Fly Postgres, the schema is Postgres-portable, so the migration is a
CSV export + `\copy` import:

```bash
sqlite3 /data/transcripts.db \
  -cmd ".headers on" -cmd ".mode csv" \
  -cmd ".output transcripts.csv" \
  -cmd "SELECT * FROM transcripts;" \
  -cmd ".quit"
# then on the Postgres side:
\copy transcripts FROM 'transcripts.csv' WITH (FORMAT csv, HEADER true);
```

Estimated cost at migration: $2-5/month for the smallest Fly Postgres.

### No retention

Transcripts are PII-redacted before they hit the DB, so there's no
liability rationale for retention. The file grows ~2 MB/year at current
call volume. If it ever becomes a problem, add a retention pass, but
that's not currently scheduled.

---

## Routine maintenance

- **Secret rotation**: see `SECRETS.md`.
- **Dependency updates**: `pip install -U -r requirements.txt` locally, run tests, deploy.
- **Prompt changes**: edit `tools/prompts/receptionist_v0.3_data_driven.md`, run a replay (`python tools/replay_transcript.py tests/replay_scenarios/mice_renter_apartment.json`) before deploying.
- **Adding a client**: copy `config/clients/clearslope.json` to a new file, edit branding/hours, create a new Fly app pointed at the new config. No code change required.

---

## Escalation

When you can't fix it within 30 minutes:

1. **Update Twilio voice webhook** to a temporary "human only" number (forward calls to the owner directly) so customers don't sit in a broken AI.
2. Post status to the team / owner.
3. Escalate to whoever set up the project last.
