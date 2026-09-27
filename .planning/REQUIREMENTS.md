# Requirements: AI Voice Receptionist

**Defined:** 2026-05-07
**Core Value:** Every inbound call gets answered and a booking attempted, no lead lost to voicemail.

## v1 Requirements

Goal: get the first external client (All Green Pest Control) onto a live pilot, then convert to paid.

### Multi-Tenancy

- [ ] **TENT-01**: System supports separate Twilio phone numbers per client
- [ ] **TENT-02**: Each client has isolated prompt, branding config, and business-hours settings
- [ ] **TENT-03**: Each client writes bookings to its own Google Sheet
- [ ] **TENT-04**: Each client's secrets (API keys, sheet ID) are stored and loaded independently
- [ ] **TENT-05**: Adding a new client requires no code changes, config file only

### Cost Protection

- [ ] **COST-01**: Each client has a configurable monthly minute ceiling enforced in app code
- [ ] **COST-02**: When a client hits their ceiling, new calls are rejected gracefully (not silently dropped)
- [ ] **COST-03**: Minute usage is tracked per client and visible in logs

### Billing

- [ ] **BILL-01**: Stripe subscription created per client at correct tier (Starter/Pro/Premium)
- [ ] **BILL-02**: One-time setup fee charged at onboarding
- [ ] **BILL-03**: Overage calculated and billed when monthly minutes exceed tier limit
- [ ] **BILL-04**: Failed payment triggers dunning flow (Stripe-native)
- [ ] **BILL-05**: Client subscription can be cancelled, pausing service

### Twilio Account

- [ ] **TWIL-01**: Twilio account upgraded off trial to production account
- [ ] **TWIL-02**: All Green pilot number provisioned under production account

### Sales Assets

- [ ] **SALE-01**: ROI one-pager built from All Green's 2025 call data, shows 215 missed calls, 33–46/mo after-hours opportunity, $2K–$6K/mo revenue recovered, pilot cost ~$3–4
- [ ] **SALE-02**: One-page sales sheet covering pricing tiers, what's included per tier, and demo link
- [ ] **SALE-03**: Demo flow (5-minute live walkthrough or Loom) that a non-technical owner can follow without explanation

## v2 Requirements

Build when a prospect names these as deal-blockers.

### Owner Visibility

- **DASH-01**: Owner can view live call list, transcripts, and booking log in a dashboard
- **DASH-02**: Owner can see monthly minute usage and trend charts
- **DASH-03**: Owner receives daily email digest ("Yesterday: 12 calls, 4 bookings, 1 emergency")

### Integrations

- **INTG-01**: Bookings pushed to owner's Slack channel (All Green requirement, confirm before building)
- **INTG-02**: Bookings pushed to ServiceTitan, HouseCall Pro, or owner's CRM
- **INTG-03**: Google Calendar availability checked before confirming appointment slot

### Outbound

- **OUTB-01**: AI places day-before reminder calls to reduce no-shows
- **OUTB-02**: AI places quarterly reactivation calls to lapsed customers

## Out of Scope

| Feature | Reason |
|---------|--------|
| Smart scheduling / route optimization | Build when a paying customer explicitly requests it; roadmap promise is enough to sell |
| Full call recording UI | Twilio records already; bundle into dashboard when dashboard is built (v2) |
| SDR or sales hire | Premature before $5K+ MRR and a documented sales motion |
| Mobile app | Not needed at this stage |
| Multi-region Fly deployment | Single region (sjc) is fine for BC/Vancouver clients |

## Traceability

Populated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| TENT-01 | Phase 1 | Pending |
| TENT-02 | Phase 1 | Pending |
| TENT-03 | Phase 1 | Pending |
| TENT-04 | Phase 1 | Pending |
| TENT-05 | Phase 1 | Pending |
| COST-01 | Phase 1 | Pending |
| COST-02 | Phase 1 | Pending |
| COST-03 | Phase 1 | Pending |
| TWIL-01 | Phase 1 | Pending |
| TWIL-02 | Phase 1 | Pending |
| SALE-01 | Phase 2 | Pending |
| SALE-02 | Phase 2 | Pending |
| SALE-03 | Phase 2 | Pending |
| BILL-01 | Phase 3 | Pending |
| BILL-02 | Phase 3 | Pending |
| BILL-03 | Phase 3 | Pending |
| BILL-04 | Phase 3 | Pending |
| BILL-05 | Phase 3 | Pending |

**Coverage:**
- v1 requirements: 18 total
- Mapped to phases: 18
- Unmapped: 0 ✓

---
*Requirements defined: 2026-05-07*
*Last updated: 2026-05-07 after initial definition*
