# Roadmap: AI Voice Receptionist

**Created:** 2026-05-07
**Milestone:** v1, First Paying Client

---

### Phase 1: Pilot Ready

**Goal:** All Green can go live on a 30-day after-hours pilot, fully isolated from Clear Slope, with a hard cost ceiling so there's no surprise API bill.

**Requirements:** TENT-01, TENT-02, TENT-03, TENT-04, TENT-05, COST-01, COST-02, COST-03, TWIL-01, TWIL-02

**Success Criteria:**
1. A second client config (`config/clients/allgreen.json`) exists and the app loads it without touching Clear Slope's config or data
2. All Green's calls write to a separate Google Sheet; Clear Slope's Sheet is unaffected
3. Setting `minute_ceiling: 200` for All Green causes calls beyond that limit to be rejected with a clear log message
4. Minute usage per client is visible in structured logs
5. Twilio account is off trial; All Green's number is provisioned and routes to the app

---

### Phase 2: Close the Deal

**Goal:** Have sales assets ready to send Soheil, an ROI one-pager built from his own data, a clean pricing sheet, and a demo he can experience in under 5 minutes.

**Requirements:** SALE-01, SALE-02, SALE-03

**Plans:** 4 plans

Plans:
- [x] 02-01-PLAN.md, Soheil-specific ROI one-pager (All Green real numbers)
- [x] 02-02-PLAN.md, 3-tier pricing sheet with payback math and tier recommendation
- [x] 02-03-PLAN.md, Live demo call instructions (2-sentence CTA, real Twilio number)
- [x] 02-04-PLAN.md, Generic ROI template derived from Soheil one-pager

**Success Criteria:**
1. ROI one-pager exists as a markdown file with All Green's real numbers (215 missed calls, 33–46/mo after-hours estimate, $2K–$6K/mo recovered revenue, ~$3–4 pilot cost)
2. Pricing sheet covers all three tiers with included minutes, overage rates, setup fee, and All Green tier recommendation
3. Demo instructions point to the live Twilio number with a when-to-call and what-to-say prompt, no Loom, no recording required
4. Generic ROI template exists with labeled placeholders replacing all All Green-specific values

---

### Phase 3: Paid Conversion

**Goal:** When the pilot ends and Soheil says yes, flip him to a paid subscription without manual invoicing, Stripe handles billing from day one.

**Requirements:** BILL-01, BILL-02, BILL-03, BILL-04, BILL-05

**Success Criteria:**
1. A Stripe subscription can be created for a client at Starter/Pro/Premium tier
2. One-time setup fee is charged at onboarding
3. Overage is calculated and billed when monthly minutes exceed tier limit
4. Failed payment triggers Stripe-native dunning (no custom code needed)
5. Cancelling a subscription disables the client's service in the app

---

## Traceability

| Requirement | Phase |
|-------------|-------|
| TENT-01 | Phase 1 |
| TENT-02 | Phase 1 |
| TENT-03 | Phase 1 |
| TENT-04 | Phase 1 |
| TENT-05 | Phase 1 |
| COST-01 | Phase 1 |
| COST-02 | Phase 1 |
| COST-03 | Phase 1 |
| TWIL-01 | Phase 1 |
| TWIL-02 | Phase 1 |
| SALE-01 | Phase 2 |
| SALE-02 | Phase 2 |
| SALE-03 | Phase 2 |
| BILL-01 | Phase 3 |
| BILL-02 | Phase 3 |
| BILL-03 | Phase 3 |
| BILL-04 | Phase 3 |
| BILL-05 | Phase 3 |

**Coverage:** 18/18 v1 requirements mapped ✓

---
*Last updated: 2026-05-08, Phase 2 complete (4/4 plans done)*
