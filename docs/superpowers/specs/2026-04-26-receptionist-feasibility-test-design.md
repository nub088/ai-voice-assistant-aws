# Clear Slope AI Receptionist, Feasibility Test (v0.1)

**Status:** Design, pre-implementation
**Date:** 2026-04-26
**Owner:** Mohd
**Implementation surface:** Vapi Composer (visual flow builder) + one Google Apps Script web app

## Goal

Prove a Vapi Composer agent can:

1. Take a real inbound call.
2. Disambiguate a probable pest through natural conversation, even when the caller doesn't know what they have.
3. Decide whether Clear Slope services that pest.
4. Capture a service request to a Google Sheet for a human to confirm.
5. End the call gracefully.

This is a **feasibility test, not production**. The point is to learn how Vapi Composer behaves on a real call with a real tool integration, on the smallest slice of the agent that exercises the risky parts. Scope expansion is deliberately deferred.

## Non-goals (explicitly out of scope for v0.1)

- Service-area gating (city/postal code checks)
- Pricing answers of any kind
- Reschedule or existing-customer flows
- Callback queue or escalation handling for edge cases (anger, language barriers, strata/liability questions, custom pricing pushback, repeat customers asking for a specific tech)
- Live human transfer
- After-hours messaging variation
- Photo-SMS pest identification
- Calendar / availability integration (no real booking, just intake)
- Any language other than English
- Production-grade phone number, a Vapi-provisioned test number is sufficient

## Grounding

The scope is grounded in a stratified Haiku-classified sample of 120 of Clear Slope's 1,346 real 2025 inbound transcripts. Key facts that shaped this design:

- ~31% of calls are out-of-scope by pest type (raccoons, beavers, otters, etc.), a polite decline path is essential, not optional.
- Callers frequently describe pests imprecisely ("small black things," "bites all over"), disambiguation through follow-up questions is a common skill the agent will need.
- New service intake (~18%) and pest-out-of-scope referral (~12%) together cover ~30% of real call volume, the v0.1 slice already maps to ~1 in 3 real calls.

## Conversation flow

One path, two exits.

```
Greet
  ↓
Listen for pest description
  ↓
Disambiguate via 1-3 short follow-up questions
  ↓
Scope check ─── out-of-scope ──→ Polite decline ──→ End
  ↓
in-scope
  ↓
Collect: name → callback phone → address → preferred date → preferred time window
(one at a time, confirming each)
  ↓
Tool call: log_request (write row to Google Sheet)
  ↓
Close
```

### Per-step behavior

1. **Greet.** "Hi, you've reached Clear Slope. What's going on?"
2. **Listen.** Caller volunteers some version of "I think I have X" or "there's something in my Y."
3. **Disambiguate.** If the caller is already clear (e.g., "wasps in my soffit"), skip. Otherwise ask 1-3 short follow-ups using the cues in the in-scope pest list below ("where in the home?", "size and colour?", "any bites?", "are you seeing them at night?"). Stop when the agent is confident enough to name a probable pest. Do not pad with more than 3 questions, if still unclear after 3, pick the closest in-scope match and proceed; the human confirming the request will catch errors.
4. **Scope check.** Match the identified pest against the in-scope list.
   - **Out-of-scope:** "Sorry, we don't service [pest]. You'd want to call a wildlife removal company for that. Have a good one." → end call.
   - **In-scope:** continue to intake.
5. **Intake.** Collect in this order, one at a time, confirming each value back to the caller before moving to the next: name, callback phone, address, preferred date, preferred time window.
6. **Log.** Single tool call: `log_request` POSTs the row to the Apps Script web app, which appends to the Sheet.
7. **Close.** "Got it. Someone from Clear Slope will call you back to confirm the time. Thanks for calling."

## In-scope pest list (baked into system prompt)

Each pest carries a short set of disambiguation cues. The agent uses these both to recognize the pest from a vague description and to ask the right follow-up question.

| Pest | Cues |
|------|------|
| Mice | Small droppings, scratching at night, holes chewed in food packaging |
| Rats | Larger droppings, gnaw marks, sightings around dawn/dusk |
| Bed bugs | Bites in lines of three, small reddish bugs in mattress seams, dark spots on sheets |
| Ants | Visible trails, often near sugar or water sources |
| Wasps / yellow jackets | Visible nest, aggressive flying insects, stings |
| Cockroaches | Fast-moving brown insects, often in kitchens/bathrooms, mostly nocturnal |
| Fruit flies | Small flying insects near ripe or rotting fruit |
| Carpet beetles | Small oval beetles, damage to fabrics or carpets |
| Carpenter bees | Large bees boring round holes in wood |
| Silverfish | Silver-grey wingless insects in damp areas |

Anything outside this list is out-of-scope: raccoons, squirrels, beavers, birds, otters, mites/scabies-uncertain, "I don't know what it is and I have a rash", all polite decline.

## Persona

- "Clear Slope" pronounced as two words.
- Default Vapi voice (no custom voice in v0.1).
- Warm, professional, brief.
- No filler ("absolutely!", "great question!", "I'd love to help with that!").
- When unsure, ask one short clarifying question rather than guessing.
- If the caller is upset, confused, or asks for something out-of-scope of v0.1 (pricing, billing, complaints, specific technicians, language other than English), apologize, take their name and number, and tell them someone will call back. Don't try to handle it.

## Google Sheet schema (first tab, one row per logged request)

| Column | Type | Notes |
|---|---|---|
| `timestamp` | ISO datetime | Set by the Apps Script at write time |
| `caller_name` | string | As collected |
| `callback_phone` | string | As collected; agent should confirm digits back |
| `address` | string | As collected |
| `reported_pest` | string | What the caller initially said they thought it was |
| `identified_pest` | string | What the agent settled on after disambiguation |
| `preferred_date` | string | Free-text from caller (e.g., "next Tuesday"), not normalized in v0.1 |
| `preferred_time_window` | string | Free-text (e.g., "morning", "after 4 PM") |
| `call_id` | string | Vapi's call ID, for traceability back to the recording |
| `notes` | string | Anything else the agent thinks is useful for the human confirming |

## Integrations

### Vapi tool: `log_request`

A single HTTP request tool wired to the Apps Script web app URL.

- **Method:** POST
- **URL:** `[APPS_SCRIPT_WEB_APP_URL]` (filled in once the script is deployed)
- **Body:** JSON matching the Sheet schema above
- **Response handling:** the agent should call this once near the end of the conversation; if it returns non-200, the agent should still close the call gracefully ("got it, someone will call you back") and the failure shows up in Vapi's tool-call logs for debugging. No retry logic in v0.1.

### Google Apps Script web app

Bound to the Clear Slope Sheet. Receives the POST from Vapi, appends a row to the first tab.

- Deployed as a web app, "Execute as: me", "Who has access: anyone with the link".
- The web app URL acts as the shared secret in v0.1. Treat the URL as sensitive (don't commit it to git; store in `.env` as `APPS_SCRIPT_WEB_APP_URL`).
- ~10-20 lines of JavaScript. Validates payload has the required fields, appends row, returns 200.

### Phone number

The existing Twilio free-trial number (1 number already in the user's Twilio account) is wired to Vapi via Vapi's "Bring your own Twilio number" flow. Twilio side: the number's voice webhook points to Vapi. Vapi side: SIP trunk configured with Twilio SID + auth token + number.

Twilio trial restrictions to keep in mind for the test:

- The user must verify their own mobile in Twilio before placing test calls (inbound to a trial number from an unverified caller may be blocked).
- Outbound calls from a trial number carry a Twilio disclaimer message, not relevant to inbound receptionist behavior.

When/if the project moves to production, the trial number is upgraded to a paid Twilio number (or a real business number is ported in).

## Operational data the user must provide before launch

- [x] In-scope pest list, confirmed above
- [x] Sheet ID, already in `.env` as `GOOGLE_SHEET_ID`
- [x] Sheet tab, first tab
- [x] Business name pronunciation, "Clear Slope" as two words
- [x] Voice, default
- [ ] **Apps Script web app URL**, generated when you deploy the script; add to `.env` as `APPS_SCRIPT_WEB_APP_URL`
- [ ] **Vapi test phone number**, provisioned in the Vapi dashboard once the agent exists

## Success criteria for the feasibility test

The agent passes if, on a set of ~10 real test calls (made by you / a friend / a colleague playing different caller scripts):

1. The agent correctly handles all three flow exits at least once: out-of-scope decline, in-scope happy path with a clear pest, in-scope happy path with an initially vague pest that requires disambiguation.
2. Every in-scope call results in a complete row in the Google Sheet with all required fields populated.
3. The agent never tries to handle anything outside v0.1 scope (no pricing answers, no rescheduling, no live transfer attempts).
4. The agent ends the call cleanly in every case (no infinite loops, no awkward dead air, no repeated greetings).

Anything beyond this, accent handling, interruptions, voicemail detection, latency, cost-per-call, is **observation, not pass/fail.** Note what you see and feed it into the v0.2 scope.

## Risks and known unknowns

- **Vapi Composer node-and-tool behavior is not 100% verified in this design.** Composer is a visual flow builder with LLM nodes, conditional transitions, and tool calls, the design above translates into that model cleanly, but exact node types, transition semantics, and tool-call behavior should be cross-referenced against Vapi's current docs before building. If the docs reveal a constraint that breaks the design, we adjust before building, not after.
- **Disambiguation is the hardest single behavior in this slice.** It depends on the LLM correctly mapping vague caller descriptions to the cues in the system prompt. Expect some misidentification; that's why the human confirms before booking.
- **Free-text date/time** (no normalization) means downstream the human has to interpret "next Tuesday afternoon" themselves. Acceptable for the test; would be a v0.2 candidate for a date-parsing tool.
- **No service-area gating** means out-of-area callers will get logged as in-scope requests. The human confirming will catch this. For a test this is fine; for production it's a major inefficiency.

## What comes after a successful feasibility test (not in v0.1)

In rough priority order:

1. Service-area gating (drops a chunk of wasted human-confirmation work)
2. Reschedule / existing-customer flow
3. Callback queue for edge cases (the real escalation paths from the call analysis)
4. Pricing answers (with a price list provided)
5. After-hours messaging variation
6. Date/time normalization
7. Live human transfer (the "C" option from the original escalation discussion)
8. Photo-SMS pest ID
9. Calendar/scheduling system integration (replaces "intake into Sheet" with real booking)
10. Multi-language

Each of these is independently shippable on top of v0.1.
