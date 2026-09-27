# Secrets

Where every credential lives, who reads it, and how to rotate it.

## Source of truth

**Production secrets live in Fly.** No `.env` file is read in production, all values come from `fly secrets`. The local `.env` is for development only and is gitignored.

List current secrets:
```
fly secrets list -a clearslope-receptionist
```

## Secret inventory

| Name | Used by | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | `voice_pipeline.py` (Claude Haiku LLM) | Receptionist must always use Haiku, never Opus/Sonnet |
| `DEEPGRAM_API_KEY` | `voice_pipeline.py` (STT) | |
| `CARTESIA_API_KEY` | `voice_pipeline.py` (TTS) | |
| `CARTESIA_VOICE_ID` | `voice_pipeline.py` | The Sarah voice. Per-tenant if/when we add multi-client. |
| `TWILIO_ACCOUNT_SID` | `sms_notifier.py` | |
| `TWILIO_AUTH_TOKEN` | `voice_routes.py` (signature validation), `sms_notifier.py` | Required in prod, server returns 500 without it unless `ALLOW_UNSIGNED_TWILIO=1` is also set (dev only) |
| `TWILIO_SMS_FROM` | `sms_notifier.py` | The customer-facing SMS sender number |
| `OWNER_NOTIFY_NUMBER` | `sms_notifier.py` | Where emergency-urgency intakes get pushed |
| `GOOGLE_SHEET_ID` | `sheets_writer.py` | Booking intake sheet. Per-tenant for multi-client (see `TODO(multi-client)` in `sheets_writer.py:241`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | `entrypoint.sh` materializes to `/app/google-service-account.json` | The full service-account JSON, stored as a single Fly secret string |
| `PUBLIC_BASE_URL` | `voice_routes.py:_public_base_url` | `clearslope-receptionist.fly.dev`, used to build the WSS Stream URL Twilio connects to |
| `DEMO_USERNAME` / `DEMO_PASSWORD` | `main.py` (text-chat demo basic auth) | Not used by the voice path. Replace before exposing the demo widget broadly. |
| `SENTRY_DSN` *(optional)* | `main.py:_init_sentry` | If unset, Sentry is a no-op. Set to enable error reporting. `SENTRY_ENV` (default `production`) and `SENTRY_TRACES_SAMPLE_RATE` (default `0.0`) tune it. |
| `LOG_FORMAT` *(optional)* | `logging_config.py` | `json` (default, prod) or `text` (legacy human-readable, local dev). |
| `LOG_LEVEL` *(optional)* | `logging_config.py` | `INFO` default. |
| `ALLOW_UNSIGNED_TWILIO` *(optional, dev only)* | `voice_routes.py` | When `1`, lets the voice endpoint accept requests without a Twilio auth token. Never set in production. |

## Local files (dev only, gitignored)

These are checked-in `.gitignore`'d and **must not** be redeployed:

- `google-service-account.json`, the GCP service account key file. Used locally for development against the dev Sheet. The same JSON content is stored as `GOOGLE_SERVICE_ACCOUNT_JSON` in Fly secrets for production. Rotate both together.
- `client_secret_*.json`, Google OAuth desktop client (used for one-time OAuth flows, not the runtime). Not used by the voice pipeline.
- `token.json`, gitignored, materialized by OAuth flows when run locally.
- `.env`, local-only env vars for development. Must not be committed.

## Rotation procedures

**Anthropic / Deepgram / Cartesia / Twilio**: rotate in their respective dashboards, then:
```
fly secrets set ANTHROPIC_API_KEY=<new> -a clearslope-receptionist
```
Fly redeploys the app on `secrets set`. Verify with a test call within ~2 min.

**Google service account key**:
1. Create a new key in GCP Console → IAM → Service Accounts → keys
2. Download the JSON
3. `fly secrets set GOOGLE_SERVICE_ACCOUNT_JSON="$(cat downloaded-key.json)" -a clearslope-receptionist`
4. Replace `google-service-account.json` locally with the same content
5. Delete the old key in GCP Console once a test call succeeds on the new one

**Twilio auth token**:
- Twilio supports a primary + secondary token. Promote secondary to primary, regenerate the secondary, update Fly secret, verify.

**Google Sheet ID**: not really a "secret" but is per-tenant, only changes when onboarding a new client.

## What is NOT a secret

These can be in code or fly.toml safely:
- Fly app name (`clearslope-receptionist`)
- Region (`sjc`)
- Voice model name, prompt version, taxonomy file path
- Any URL ending in `.fly.dev` (the host is public)

## Onboarding a new operator

To get a second person able to deploy:
1. Add them to the Fly org (`fly orgs invite ...`)
2. Add them to the GCP project as Editor on the Sheets API + service account
3. Add them as a Twilio admin
4. They run `fly secrets list -a clearslope-receptionist` to verify access, they don't need the local `.env` for production work

## Multi-client note

Most secrets above are *singletons* today, marked with `TODO(multi-client)` comments at `sheets_writer.py:241` and `sms_notifier.py:134`. The current path forward is per-app-per-client Fly deploys (e.g. `bobsplumbing-receptionist` would have its own `GOOGLE_SHEET_ID`, `TWILIO_SMS_FROM`, `OWNER_NOTIFY_NUMBER`, `CARTESIA_VOICE_ID`) rather than a tenant routing layer. See the multi-client analysis at `~/.claude/plans/can-you-explain-to-jolly-yeti.md`.
