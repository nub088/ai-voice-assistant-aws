---
phase: "02-close-the-deal"
plan: "01"
subsystem: "sales-assets"
tags: ["roi", "one-pager", "soheil", "all-green", "sales"]
dependency_graph:
  requires: []
  provides: ["02-roi-soheil.md"]
  affects: ["02-04-PLAN.md (generic template derives from this)"]
tech_stack:
  added: []
  patterns: ["data-forward copywriting", "owner-to-owner tone"]
key_files:
  created:
    - .planning/phases/02-close-the-deal/02-roi-soheil.md
  modified: []
decisions:
  - "Setup fee framed as waived/credited for early pilot conversion, lowers friction without discounting MRR"
  - "No Loom or video reference per locked context decision"
  - "Month-to-month framing explicit to reduce commitment objection"
metrics:
  duration: "< 5 minutes"
  completed: "2026-05-08"
  tasks_completed: 1
  tasks_total: 1
  files_created: 1
  files_modified: 0
---

# Phase 2 Plan 01: Soheil ROI One-Pager Summary

**One-liner:** Soheil-specific ROI one-pager using All Green's real 215-call dataset with $2K–$6K recovery range and $199/mo Starter ask.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Write Soheil-specific ROI one-pager | c509803 | .planning/phases/02-close-the-deal/02-roi-soheil.md |

## Output

**File:** `.planning/phases/02-close-the-deal/02-roi-soheil.md`
**Word count:** 301 (limit: 450)

All locked numbers present:
- 215 missed/after-hours calls
- 33–46 after-hours calls/month
- $2,000–$6,000/month recovery range
- $3–4 pilot cost
- $199/month Starter price
- 500 included minutes

## Deviations from Plan

None, plan executed exactly as written.

## Known Stubs

None. All numbers are real (locked from call data and PRICING.txt). No placeholder content.

## Threat Flags

None. Document contains intentionally shared prospect-facing data; no PII; no new network surface.

## Self-Check: PASSED

- [x] `.planning/phases/02-close-the-deal/02-roi-soheil.md` exists
- [x] Commit c509803 exists
- [x] "215" present in file
- [x] "Loom" and "recording" absent
- [x] Word count 301 < 450
- [x] "Soheil" and "All Green" present
