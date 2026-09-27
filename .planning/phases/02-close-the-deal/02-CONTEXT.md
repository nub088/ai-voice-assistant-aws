---
phase: "02"
title: "Close the Deal"
date: "2026-05-07"
status: "ready-for-planning"
---

# Phase 2 Context: Close the Deal

## Domain

Produce 3 sales assets that convert Soheil (All Green Pest Control) from pilot to paid subscriber, and serve as templates for future prospects. No code. Pure content.

Deliverables:
1. **ROI one-pager**, Soheil-specific version + generic template
2. **Pricing sheet**, 3-tier breakdown with what's included
3. **Demo instructions**, 2-sentence call-to-action: Soheil calls the live number

## Decisions

### One-Pager
- **Produce two versions**: one personalized for Soheil (All Green's real numbers throughout), one generic template for future pest control / home services prospects
- **Format**: markdown files, user pastes into Google Docs and shares links
- **Tone**: data-forward, not story-first, Soheil is an owner, he responds to numbers
- **Numbers to use** (locked from ROADMAP.md):
  - 215 missed calls analyzed from All Green's 2025 call data
  - 33–46 after-hours calls/month estimated opportunity
  - $2,000–$6,000/month recovered revenue potential
  - ~$3–4 API cost for the 30-day pilot (200-minute ceiling)
  - Pilot is free / terminable by either party

### Demo
- **No recording needed**, Soheil calls the live number himself
- **What to include**: the number to call + a 2-sentence prompt ("Call after 5pm or on weekends. Ask to book a pest inspection.")
- The product demos itself, AI answers, books a lead, confirms via SMS
- No Loom, no screen recording, no script to follow

### Pricing Sheet
- **Tiers** (from PRICING.txt, locked):

  | Tier    | Price/mo | Included min | Overage   |
  |---------|----------|--------------|-----------|
  | Starter | $199     | 500          | $0.40/min |
  | Pro     | $499     | 1,500        | $0.30/min |
  | Premium | $999     | 4,000        | $0.25/min |

- **Setup fee**: $299–$499 one-time (prompt customization, Twilio provisioning, Sheets integration, onboarding call)
- **All Green's likely tier**: Starter ($199/mo), pest control after-hours volume fits within 500 min easily
- Sheet should include ROI math inline: at Starter tier, one recovered booking/month pays for 3+ months

### Delivery
- Produce all 3 files as markdown, user decides how to package/send
- No landing page or Drive folder setup required from planner/executor

## Canonical References

- `PRICING.txt`, full tier breakdown, cost model, margin analysis
- `.planning/ROADMAP.md`, Phase 2 success criteria and ROI numbers
- `.planning/PROJECT.md`, product description, target market
- `notes/`, pitch notes and "how to pitch with 0 data" context

## Code Context

No existing code is relevant to this phase. All deliverables are documents.

## Deferred Ideas

- **Loom recording**, Discussed and rejected. Voice product demos itself via a live call.
- **Landing page / hosted sales site**, Future phase if more prospects need a self-serve path.
- **Smart scheduling add-on pitch**, Mentioned in PRICING.txt as roadmap currency ("Q3 roadmap"). Can reference in pricing sheet as "coming soon" but do not spec it.
