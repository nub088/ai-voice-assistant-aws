# AI Voice Receptionist

## What This Is

An AI phone receptionist for home services businesses (pest control, HVAC, plumbing, roofing) that answers every inbound call, qualifies the lead, and logs a booking to Google Sheets, 24/7, including after hours. Built on Pipecat + Twilio + Deepgram + Cartesia + Claude Haiku, deployed on Fly.io. Currently in soft launch for Clear Slope Pest Control (Vancouver); targeting All Green Pest Control as the first external paying client.

## Core Value

Every inbound call gets answered and a booking attempted, no lead lost to voicemail.

## Requirements

### Validated

- ✓ Voice pipeline (Twilio → Deepgram STT → Haiku → Cartesia TTS → Twilio), soft launch
- ✓ Per-client branding via `config/clients/*.json` (name, AI name, hours, timezone, prompt path)
- ✓ Google Sheets booking intake via `log_request` function tool
- ✓ Post-call SMS: customer confirmation + owner emergency alert
- ✓ Business-hours / after-hours context injected into system prompt
- ✓ PII redaction on stored data
- ✓ Twilio webhook signature enforcement
- ✓ Rate limiting, auth (`/auth/demo`), `/healthz` with dependency checks
- ✓ JSON structured logging + optional Sentry
- ✓ Weekly call digest CLI (`tools/llm_proxy/digest_cli.py`)
- ✓ Replay/test harness (`tools/replay_transcript.py`)
- ✓ Deployed on Fly.io (`clearslope-receptionist`)

### Active

- [ ] Per-client minute ceiling, hard cost cap in app code (Deepgram/Cartesia can't enforce one; critical for free pilots)
- [ ] Per-client tenancy, separate Twilio numbers, prompts, secrets, and Sheets per client
- [ ] Free 30-day after-hours pilot offer for All Green (terminable by either party)
- [ ] ROI one-pager using All Green data, 33–46 after-hours calls/mo captured, 12–16 new bookings, ~$2K–$6K recovered revenue vs. ~$3–4 API cost
- [ ] Sales sheet, pricing tiers, ROI math, demo link
- [ ] Demo flow, 5-minute live or Loom walkthrough a non-technical owner can follow
- [ ] Stripe billing, subscriptions, overage, setup fee, dunning (needed before paid conversion)
- [ ] Twilio account upgrade off trial

### Out of Scope

- Slack / CRM integration, wait until All Green accepts the pilot and names it as a requirement
- Client dashboard (call list, transcripts, usage charts), build when a prospect names it as a deal-blocker
- Smart scheduling / route optimization, wait for explicit customer pull; roadmap promise is enough to sell
- Outbound calling (reminders, reactivation), future milestone after first paying client
- Hiring an SDR or sales rep, premature before $5K+ MRR and a documented sales motion
- Full call recording UI, Twilio records already; bundle into dashboard when dashboard is built

## Context

**Sales motion:** Free 30-day after-hours pilot. Forward calls outside business hours (5pm–8am + weekends) to the AI. Owner risk is zero, those calls went to voicemail before. Termination clause protects against API cost blowout (realistically $3–5/month at All Green's call volume).

**All Green data (from 2025 PPC analysis, 1,561 tracked calls):**
- 215 calls never answered (14%)
- 42% of answered calls book an appointment ($220 cost-per-booking)
- 25–35% of calls estimated after-hours → 390–546/yr capturable
- Recovered revenue estimate: $28K–$68K/yr at current booking rate and ticket size
- Their ad spend: $142,400/yr, making a $2K/mo receptionist a rounding error

**Prospect status:** Soheil (All Green contact) has gone warm-cold. Has the data, hasn't committed. Approach: share the after-hours ROI estimate, offer a zero-risk 30-day pilot on after-hours line only.

**Tech constraints:**
- Receptionist model locked to Claude Haiku 4.5 on call path, Sonnet/Opus would 3–5× the per-call cost
- Deepgram, Cartesia, and Fly.io have no hard billing caps, minute ceilings must be enforced in app code
- Twilio trial account limits active numbers and has unverified-caller restrictions, must upgrade before onboarding external client

**Pricing (decided):**
- Starter $199/mo (500 min), Pro $499/mo (1,500 min), Premium $999/mo (4,000 min)
- Setup fee $299–$499 one-time
- Gross margin ~80–87% at full utilization

## Constraints

- **Cost**: No hard caps at vendor level, per-client minute ceiling must be app-enforced before any external pilot
- **Twilio**: Trial account blocks production use for external clients, upgrade is a hard blocker
- **Trust**: Warm relationship required before cold-pitch converts; All Green needs a low-risk entry point
- **Build discipline**: No feature ships without a paying customer or signed LOI requesting it

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Haiku locked on call path | 3–5× cost savings over Sonnet; latency also better for real-time voice | ✓ Good |
| Subscription pricing (not per-minute) | SMB owners buy monthly line items; matches Smith.ai/Ruby/Synthflow pricing model |, Pending |
| Per-client config via JSON files | Enables multi-client without code changes; branding/hours/prompt fully configurable | ✓ Good |
| Build to demand (no speculative features) | Every speculative build gets wrong abstractions; roadmap promises are valid sales currency | ✓ Good |
| 30-day after-hours-only pilot | Eliminates owner risk (calls went to voicemail anyway); limits API cost exposure to ~$3–5 |, Pending |
| Slack/CRM integration deferred | Don't build until pilot accepted and integration confirmed as requirement |, Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition:**
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone:**
1. Full review of all sections
2. Core Value check, still the right priority?
3. Audit Out of Scope, reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-05-07 after initialization*
