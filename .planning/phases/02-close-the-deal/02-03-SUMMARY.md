---
phase: "02-close-the-deal"
plan: "03"
subsystem: sales-assets
tags: [demo, cta, twilio, live-call]
dependency_graph:
  requires: []
  provides: [02-demo-instructions.md]
  affects: []
tech_stack:
  added: []
  patterns: [markdown-deliverable]
key_files:
  created:
    - .planning/phases/02-close-the-deal/02-demo-instructions.md
  modified: []
decisions:
  - "Used formatted number +1 (813) 607-6475 with raw form +18136076475 inline to satisfy both readability and grep verification"
metrics:
  duration: "< 5 minutes"
  completed: "2026-05-08"
---

# Phase 02 Plan 03: Demo Instructions Summary

**One-liner:** Live demo CTA, 2-sentence call instruction pointing to +18136076475 (Clear Slope production Twilio line), no video, no script, AI demos itself.

## What Was Built

`.planning/phases/02-close-the-deal/02-demo-instructions.md`, a minimal markdown file Soheil can read in 10 seconds. Tells him to call the live number after 5pm or on weekends and ask to book a pest inspection. The AI answers, attempts a booking, and sends SMS confirmation. No human involvement required.

## Tasks

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Write demo call instructions | f76a20d | .planning/phases/02-close-the-deal/02-demo-instructions.md |

## Acceptance Criteria Verification

- [x] File exists at `.planning/phases/02-close-the-deal/02-demo-instructions.md`
- [x] Contains "+18136076475"
- [x] Contains "5pm" (when-to-call)
- [x] Contains "weekend" (when-to-call)
- [x] Contains "pest inspection" (what-to-say)
- [x] Contains "SMS" (confirmation mention)
- [x] Does NOT contain "Loom"
- [x] Does NOT contain "recording"
- [x] Does NOT contain "video"
- [x] Does NOT contain "script"
- [x] Word count: 99 (under 120)

## Deviations from Plan

**1. [Rule 2 - Missing Critical] Added raw number format alongside formatted number**
- **Found during:** Verification
- **Issue:** Plan's `must_have` required `+18136076475` (raw form); file initially only contained `+1 (813) 607-6475` (formatted). Grep returned 0 matches.
- **Fix:** Added `(+18136076475)` inline after the formatted number. Both forms now present.
- **Files modified:** `02-demo-instructions.md`
- **Commit:** f76a20d (same task commit)

## Threat Surface Scan

No new network endpoints, auth paths, or schema changes. The Twilio number is already published (Phase 1). No new threat flags.

## Self-Check: PASSED

- File exists: FOUND
- Commit f76a20d: FOUND
- All acceptance criteria: PASSED
