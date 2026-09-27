---
phase: "02-close-the-deal"
plan: "04"
subsystem: "sales-assets"
tags: [template, roi, sales, generalization]
dependency_graph:
  requires: ["02-01"]
  provides: ["02-roi-template.md"]
  affects: []
tech_stack:
  added: []
  patterns: ["fill-in-the-blank template with HTML comment instructions"]
key_files:
  created:
    - .planning/phases/02-close-the-deal/02-roi-template.md
  modified: []
decisions:
  - "Examples in fill-in block use generic names (Mike, Acme Pest Control) not All Green/Soheil"
  - "Annual recovery range line left without calculated values, prospect fills floor/ceiling, annual math is self-evident"
metrics:
  duration: "~5 minutes"
  completed: "2026-05-08"
  tasks_completed: 1
  tasks_total: 1
  files_changed: 1
---

# Phase 2 Plan 04: Generic ROI Template Summary

Generic ROI one-pager template with 12 labeled placeholders replacing all All Green-specific values, identical structure to the Soheil version, ready to personalize for any pest control or home services prospect in under 5 minutes.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Derive generic ROI template from Soheil one-pager | 7f49220 | .planning/phases/02-close-the-deal/02-roi-template.md |

## Deviations from Plan

**1. [Rule 1 - Bug] Removed All Green / Soheil from comment block examples**
- **Found during:** Task 1 acceptance check
- **Issue:** Plan said "Do NOT reference All Green or Soheil by name anywhere in the template", initial draft had them as examples inside the HTML comment
- **Fix:** Replaced with generic examples (Mike / Acme Pest Control)
- **Files modified:** 02-roi-template.md
- **Commit:** 7f49220

## Acceptance Criteria Verification

| Check | Result |
|-------|--------|
| File exists at target path | PASS |
| Contains [MISSED_CALLS] | PASS |
| Contains [PROSPECT_NAME] | PASS |
| Contains [BUSINESS_NAME] | PASS |
| Contains [AFTER_HOURS_MIN] | PASS |
| Contains [RECOVERY_FLOOR] | PASS |
| Contains [PILOT_COST] | PASS |
| Contains [INDUSTRY] | PASS |
| Contains [AVG_TICKET_LOW] | PASS |
| Does NOT contain "215" | PASS |
| Does NOT contain "All Green" | PASS |
| Does NOT contain "Soheil" | PASS |
| Does NOT contain "33–46" literal | PASS |
| Contains "199" (product constant) | PASS |
| Contains HTML comment fill-in block | PASS |

## Known Stubs

None, template is intentionally placeholder-based; no stub data flows to rendering.

## Threat Flags

None, template contains no PII; placeholders carry no real prospect data.

## Self-Check: PASSED

- 7f49220 confirmed in git log
- .planning/phases/02-close-the-deal/02-roi-template.md confirmed on disk
