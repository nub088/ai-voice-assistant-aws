# Phase 1: Pilot Ready - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in 01-CONTEXT.md, this log preserves the alternatives considered.

**Date:** 2026-05-07
**Phase:** 1-Pilot Ready
**Areas discussed:** Secrets per client, Minute ceiling tracking, Twilio account structure, Deployment topology

---

## Secrets Per Client

| Option | Description | Selected |
|--------|-------------|----------|
| Prefixed env vars | CLEARSLOPE_*, ALLGREEN_* per client; prefix specified in client JSON | ✓ |
| JSON config → env var mapping | JSON lists which env var names to read; more indirection | |
| AWS Secrets Manager | Fetch from AWS at startup; matches stated production path | |

**User's choice:** Prefixed env vars

---

| Option | Description | Selected |
|--------|-------------|----------|
| Share one service account | One credentials.json; both sheets shared with same service account email | ✓ |
| Separate service account per client | Isolated but two credential files to manage | |

**User's choice:** Share one service account

---

| Option | Description | Selected |
|--------|-------------|----------|
| Inbound Twilio number (To) | POST /twilio/voice includes To field; map To → client | ✓ |
| Subdomain / URL path | Separate webhook paths per client | |
| You decide | No strong preference | |

**User's choice:** Inbound Twilio number (To)

---

| Option | Description | Selected |
|--------|-------------|----------|
| Derived at startup from client JSONs | Scan config/clients/*.json; no separate registry file | ✓ |
| Explicit registry file | config/registry.json maps number → client_id | |

**User's choice:** Derived at startup from client JSONs

---

## Minute Ceiling Tracking

| Option | Description | Selected |
|--------|-------------|----------|
| Reset to zero on restart | In-memory counter; simple; acceptable for pilot | ✓ |
| Survive restarts | File-backed counter; eliminates restart loophole | |

**User's choice:** Reset to zero on restart (in-memory)

---

| Option | Description | Selected |
|--------|-------------|----------|
| Reject at call start only | Check before pipeline; brief TTS + hangup if hit | ✓ |
| Cut off mid-call too | Also terminate long active calls; more complex | |

**User's choice:** Reject at call start only

---

| Option | Description | Selected |
|--------|-------------|----------|
| Call duration at hangup | Wall-clock seconds, converted to minutes, added at end | ✓ |
| Real-time during call | Increment every N seconds (only needed for mid-call cutoff) | |

**User's choice:** Call duration at hangup

---

| Option | Description | Selected |
|--------|-------------|----------|
| Client JSON config | minute_ceiling field in allgreen.json; null = no ceiling | ✓ |
| Env var (ALLGREEN_MINUTE_CEILING) | Part of prefixed env var pattern | |

**User's choice:** Client JSON config

---

## Twilio Account Structure

| Option | Description | Selected |
|--------|-------------|----------|
| Shared account, new number | One TWILIO_AUTH_TOKEN; same account | ✓ |
| Twilio subaccount | Per-client SID + auth token; full isolation | |

**User's choice:** Shared account, new number
**Notes:** User noted subaccounts as future upgrade path when scaling. To be noted for later.

---

| Option | Description | Selected |
|--------|-------------|----------|
| Stay global (one auth token) | One TWILIO_AUTH_TOKEN; simple | |
| Client-aware validator | Auth token threaded through client registry; zero refactor for subaccounts | ✓ |

**User's choice:** Client-aware validator (future-proofing)

---

| Option | Description | Selected |
|--------|-------------|----------|
| Manual provisioning | Provision in Twilio console; code just routes correctly | ✓ |
| Automate via Twilio API | Script to purchase + configure numbers | |

**User's choice:** Manual provisioning for now
**Notes:** Automation noted as future path.

---

## Deployment Topology

| Option | Description | Selected |
|--------|-------------|----------|
| Same Fly.io app | One app, two clients; simpler ops | ✓ |
| Separate Fly.io app | Full isolation; doubles ops overhead | |

**User's choice:** Same Fly.io app (clearslope-receptionist)

---

| Option | Description | Selected |
|--------|-------------|----------|
| CORS only for demo path, leave as-is | Voice is server-to-server; no All Green frontend in Phase 1 | ✓ |
| Add allgreen CORS origin | Future-proofing | |

**Claude's decision:** Leave CORS as-is (user asked for clarification on CORS; Claude explained it's irrelevant for Phase 1 voice webhooks)

---

| Option | Description | Selected |
|--------|-------------|----------|
| Keep clearslope-receptionist | No rename; no Twilio webhook changes | ✓ |
| Rename to ai-receptionist | Better reflects multi-client reality; requires Twilio webhook update | |

**User's choice:** Keep clearslope-receptionist

---

## Claude's Discretion

- CORS handling: Left as-is. User unfamiliar with CORS; Claude explained it's irrelevant for Phase 1 (voice webhooks are server-to-server).

## Deferred Ideas

- Twilio subaccounts, per-client SID + auth token; upgrade path for scale
- Automated Twilio number provisioning, script via Twilio API
- Persistent minute counter, file-backed; add if restart-reset becomes a concern
- All Green demo UI / CORS, add origin if browser frontend is built
