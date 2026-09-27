# Staging Environment

A second Fly app pointed at a Twilio test number, used to validate prompt changes, dependency upgrades, and infra changes before they touch real customers. Cost is ~$0/month at idle (single shared-cpu-1x machine, suspend-when-idle) and a Twilio toll-free or local test number (~$1/month).

## One-time setup

### 1. Create the staging Fly app

```
fly apps create clearslope-receptionist-staging
```

### 2. Copy the production config

In the repo:

```
cp infra/fly/fly.toml infra/fly/fly.staging.toml
```

Edit `infra/fly/fly.staging.toml`:
- `app = "clearslope-receptionist-staging"`
- Optional: drop `min_machines_running = 1` to let the machine suspend when idle (saves money).

### 3. Set staging secrets

Same shape as prod, but:
- **`GOOGLE_SHEET_ID`**, point at a *separate* sheet (`Clear Slope, Staging Intake`) so test calls don't pollute the real booking queue.
- **`TWILIO_SMS_FROM`**, point at a Twilio test number. Test calls will SMS the staging number, not the real customer-facing one.
- **`OWNER_NOTIFY_NUMBER`**, point at the developer's number, not the owner's. Don't wake the owner on test calls.
- **`SENTRY_ENV=staging`** if Sentry is enabled, so staging errors don't blend into prod alerts.
- **`CARTESIA_VOICE_ID`**, same as prod is fine (the voice doesn't need to differ).
- All other secrets, copy from prod:

```
for k in ANTHROPIC_API_KEY DEEPGRAM_API_KEY CARTESIA_API_KEY CARTESIA_VOICE_ID \
         TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN PUBLIC_BASE_URL \
         GOOGLE_SERVICE_ACCOUNT_JSON; do
  # No way to read fly secret values back, so re-set from your local source.
  fly secrets set $k="$LOCAL_VALUE" -a clearslope-receptionist-staging
done
```

Set the staging-specific values:

```
fly secrets set \
  GOOGLE_SHEET_ID="<staging-sheet-id>" \
  TWILIO_SMS_FROM="+1<staging-twilio-number>" \
  OWNER_NOTIFY_NUMBER="+1<your-dev-number>" \
  PUBLIC_BASE_URL="clearslope-receptionist-staging.fly.dev" \
  -a clearslope-receptionist-staging
```

### 4. Provision a Twilio test number

In Twilio Console → Phone Numbers → Buy a Number, pick a number in your area. Configure:
- **Voice & Fax → "A call comes in"**: `https://clearslope-receptionist-staging.fly.dev/twilio/voice` (HTTP POST)
- **Messaging → "A message comes in"**: leave default (we don't handle inbound SMS)

### 5. Deploy

```
fly deploy --config infra/fly/fly.staging.toml
```

Verify:

```
curl https://clearslope-receptionist-staging.fly.dev/healthz
# expect: {"status":"ok",...}
```

Place a test call to the staging number. Verify:
- SMS confirmation lands on your dev number (not the owner's).
- Booking row lands in the staging sheet (not prod).

## Day-to-day usage

### Test a prompt change

1. Edit `tools/prompts/receptionist_v0.3_data_driven.md` on a feature branch.
2. Run replays locally first: `python tools/replay_transcript.py tests/replay_scenarios/mice_renter_apartment.json`. Catch obvious regressions.
3. Deploy to staging: `fly deploy --config infra/fly/fly.staging.toml`.
4. Place a real test call. Verify booking row + SMS look right.
5. Merge + deploy to prod: `fly deploy` (uses default `fly.toml`).

### Test a dependency upgrade

Same flow, staging deploy first, place 2-3 test calls, watch logs:

```
fly logs -a clearslope-receptionist-staging
```

### Test a new client config

1. Create `config/clients/<new-client>.json`.
2. Set `CLIENT_CONFIG_PATH=config/clients/<new-client>.json` as a fly secret on staging (or modify `prompt_loader.py` to read from this env var if not yet wired).
3. Deploy staging. Test calls render the new branding.

## Notes

- **Staging is not a fork.** It runs the same code as prod (same git ref unless you deploy a feature branch). The only differences are the Sheet ID, SMS from-number, and OWNER_NOTIFY_NUMBER.
- **Don't share the staging number publicly.** It has lower rate limits and no Twilio fraud protection on it.
- **Staging logs may contain real PII** if you call from your own phone. Treat them as you would prod.
