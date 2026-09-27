# GDPR Gap Analysis, Selling This Product Into the EU

This is the engineering, contractual, and operational work that must be done before the AI receptionist can be deployed for a client whose callers are located in the European Economic Area, the United Kingdom, or Switzerland. It is *not* needed for the current Canadian-only deployments.

Not legal advice. A privacy lawyer should review before the first EU customer goes live.

---

## Scope and assumptions

- **Product**: multi-tenant AI receptionist defined in `config/clients/<client>.json`, deployed on Fly.io.
- **Roles**: each operator (client) is the **data controller** for their callers' personal data. Clear Slope Digital is the **data processor** acting on the controller's instructions.
- **Trigger**: this checklist activates the first time we sign a client whose callers are located in the EEA, UK, or Switzerland.
- **Current state**: the system is operationally suitable for Canadian PIPEDA / BC PIPA deployments. The gaps below are GDPR-specific.

---

## 1. Architecture changes

### 1.1 Move compute to an EEA region

Today: the app runs in `sjc` (San Jose). Even ephemeral processing of voice and transcripts touches US soil.

Change: deploy a second app instance (or move the existing one for EU-only clients) to Fly's `fra` (Frankfurt) or `ams` (Amsterdam) region. One-line change to `fly.toml` primary region plus a `fly regions set fra`. Expect ~150 ms more round-trip latency for any non-EU caller hitting that app, acceptable when the callers are themselves in the EU.

Per the multi-client approach in `SECRETS.md`, EU clients should get their own Fly app (e.g. `acme-eu-receptionist`) pinned to `fra`, not be shared with US/CA clients.

### 1.2 Choose EU-resident sub-processors where the option exists

- **Twilio**: provision the operator's number in an EU region (e.g. UK, Germany) and ensure media stays in-region with regional Voice + Media Streams.
- **Deepgram**: enterprise tier offers EU residency. Confirm before signing the client.
- **Anthropic**: as of the 2025 EU launch, Anthropic offers EU-residency processing on appropriate plans. Confirm tier supports the receptionist's traffic before launch.
- **Cartesia**: smallest provider, least mature on EU residency. If unavailable, swap TTS to Azure Speech (EU regions) or ElevenLabs (EU residency on enterprise) for EU clients.
- **Google Sheets**: enable Google Workspace data residency for the operator's Sheet to keep it in the EU.

### 1.3 Update `voice_routes.py` to enforce client-region pinning

A request from a client config flagged `region: "eu"` must fail closed if any sub-processor key in use does not have a verified EU residency configuration. Add a startup check in `voice_pipeline.py` that refuses to start an EU client if region-pinning is incomplete.

---

## 2. Per-sub-processor due diligence

For each US-based sub-processor that remains in the chain for an EU client, do the following **before** the client goes live. Track results in a private spreadsheet, not the repo.

| Provider | Confirm | Sign |
|---|---|---|
| Twilio | EU-U.S. Data Privacy Framework certification (active) | DPA, SCCs as fallback |
| Deepgram | DPF status or SCCs | DPA, SCCs (Module 3) |
| Anthropic | DPF status or SCCs | DPA, SCCs (Module 3) |
| Cartesia | DPF status or SCCs, newest in the stack, highest risk | DPA, SCCs (Module 3); if absent, swap provider |
| Google | DPF certification (active) | DPA |
| Fly.io | DPF status; confirm `fra`/`ams` data residency in their DPA | DPA |

For each one without active DPF certification, perform a written **Transfer Impact Assessment** before relying on SCCs alone. Template available from EDPB Recommendations 01/2020. Keep the TIA on file, supervisory authorities can request it.

---

## 3. Contracts and paperwork

### 3.1 Article 28 DPA with the client (we are the processor)

Template a Data Processing Agreement that the operator signs before onboarding. Must include the Article 28(3) elements:

- Subject matter and duration of processing
- Nature and purpose of processing
- Type of personal data and categories of data subjects
- Obligations and rights of the controller
- Confidentiality
- Security measures (Article 32), reference our security posture document
- Sub-processor list and our notification commitment for changes
- Cooperation with data-subject requests
- Assistance with DPIAs and breach notifications
- Return or deletion of data on contract end
- Audit rights

Cross-reference: `SECRETS.md` for the security model, this document for sub-processor list.

### 3.2 Sub-processor DPAs (we are the controller of our own data, processor of our clients')

Sign the DPA each provider offers. Most are clickwrap; Twilio, Google, Anthropic require account-level signing. File copies in a private DPA folder, not the repo.

### 3.3 Records of Processing Activities (Article 30)

We must maintain a written record of:
- Categories of processing carried out on behalf of each controller
- Categories of data subjects and data
- Categories of transfers, including transfer mechanism
- Security measures

Keep this as a structured Markdown doc per client outside the repo, refreshed at each client onboard / sub-processor change.

### 3.4 DPIA (Article 35)

Voice processing + AI + automated triage + special-category data exposure = almost certainly requires a DPIA per the EDPB's Article 35 guidelines (criterion: "innovative use of technology" + "data processed on a large scale" + "use of new technological or organizational solutions"). Write one DPIA template per client deployment type (not per client). Cover:

- Systematic description of the processing
- Necessity and proportionality
- Risks to data subjects
- Measures to mitigate (sub-processor DPAs, region pinning, opt-in consent, human handoff, etc.)

Re-do annually or whenever the system materially changes.

### 3.5 EU representative (Article 27)

If the operator has no EU establishment but offers services to EU callers, they must appoint an EU representative. We do not provide this, recommend a third-party service (e.g. EDPO, VeraSafe). Document the representative in the operator's privacy notice.

---

## 4. Code changes

These are concrete changes to the codebase, with file pointers. Each is small individually but the bundle is what makes EU deployment defensible.

### 4.1 Recording consent, opt-in instead of "by continuing you consent"

Today the receptionist's opening line treats continued conversation as implied consent. Under EDPB guidance, GDPR consent must be as easy to refuse as to give.

Change: in `tools/prompts/receptionist_v0.3_data_driven.md` (or a new EU variant), update the opening to:

> "Hi, you've reached [BUSINESS]. This call may be recorded so we can serve you accurately. To agree, just keep talking. To decline, say 'no recording' and I'll take your request without recording. You can speak to a human any time by saying 'human'."

Wire a `no_recording_requested` flag through the pipeline so:
- Audio is not retained even ephemerally
- Transcripts are flagged for shorter retention
- Quality-review excludes that call

### 4.2 "Speak to a human", guaranteed exit

Per Article 22(2)(c), the data subject must be able to obtain human intervention. Today the prompt mentions a human handoff but does not guarantee it. Add a `human_handoff` tool and wire it to a transfer-call action via Twilio `<Dial>` or a callback flag in the booking record. Document in `RUNBOOK.md`.

### 4.3 Automated retention enforcement

Today retention is policy ("90 days for transcripts") but not enforced. Build:
- A daily cron (Fly scheduled task or a separate worker) that deletes transcripts older than 90 days from wherever they live.
- A daily cron that purges recorded audio older than 30 days (when 4.1 is implemented).
- Log lines for each deletion run with counts (for audit evidence).

### 4.4 Subject-rights tooling

Each of access / rectification / erasure / portability is currently a manual exercise. Build a script `tools/data_subject_request.py` that, given a phone number, locates and either dumps or deletes:
- Rows in the operator's Google Sheet
- Transcripts in `.tmp` and S3
- Call SIDs in logs (Fly logs retention is short, but mark calls in our long-term store)

Output a JSON receipt the operator can show the data subject.

### 4.5 Special-category data redaction

`tools/redact_pii.py` already exists. Verify it removes Article 9 categories (health, religion, ethnicity, etc.) before transcripts are retained for quality review. Add an opt-in flag in client config: `redact_special_categories: true`.

### 4.6 Logging hygiene

`fly logs` currently includes full booking payloads on `DROPPED` rows (`RUNBOOK.md` "booking is missing"). For EU clients, redact PII from log payloads before they leave the application boundary. Use the existing redaction tool or a dedicated log filter.

### 4.7 Owner-alert SMS retention

The current notice says: "Owner-alert SMS, these sit in the owner's phone indefinitely." This is not GDPR-compliant for caller data on a personal device. Two options:

1. Document the owner's phone as part of the controller's processing operations and treat the SMS retention as the controller's responsibility, covered by the operator's own retention policy.
2. Replace SMS with a portal link that expires after, e.g., 7 days.

Option 1 is the realistic short-term path. Note it in the client onboarding checklist.

### 4.8 Breach detection signal

Add a Sentry alert (or equivalent) for events that may indicate a personal-data breach: unauthorized auth, sustained 5xx on the Sheets-writer path, anomalous mass-deletion of rows. The 72-hour Article 33 clock starts when we **become aware** of the breach, which means we have to define what "aware" means operationally, see `RUNBOOK.md`.

---

## 5. Operational procedures

### 5.1 Breach response runbook

Add a section to `RUNBOOK.md`:

- Definition of "personal data breach" (Art. 4(12))
- Detection signals (4.8 above)
- Triage: severity, who is affected, what data
- Notification ladder: controller (operator) within 24h, supervisory authority within 72h via the controller, data subjects without undue delay if high risk
- Documentation requirement (Art. 33(5))

### 5.2 Sub-processor change procedure

When we change a sub-processor, every EU client must be notified at least 30 days in advance with the right to object. Add to onboarding: a notification channel (email list of operator contacts).

### 5.3 Annual review

DPIA, RoPA, sub-processor list, transfer mechanisms, review annually or on material change. Calendar reminder.

---

## 6. Explicitly out of scope for this checklist

- **Legal counsel review**, required before first EU deployment. Engage a privacy lawyer with EU GDPR practice.
- **Operator's own GDPR posture**, the operator needs their own privacy notice (use `PRIVACY_NOTICE_DRAFT.md` + the EU supplement), their own DPO if applicable, and their own EU representative. We provide tooling and a template, not their compliance.
- **Country-specific telephony rules**, Germany, France, and others have stricter call-recording rules than the GDPR baseline. Check member-state law for each EU country the operator serves.
- **EU AI Act (in force August 2026)**, adds transparency obligations for AI assistants. Already partially addressed in the privacy notice ("AI receptionist" disclosure). May add additional logging or risk-management requirements as Article 50 takes effect.

---

## Quick checklist for the first EU client

- [ ] Sign DPA with the client (we are processor)
- [ ] Sign DPA + SCCs with each sub-processor we don't already have one with
- [ ] Move compute to `fra`/`ams`; provision EU phone number
- [ ] Verify Anthropic / Deepgram / Cartesia EU-residency configuration
- [ ] Deploy EU variant of the receptionist prompt with opt-in consent (§4.1)
- [ ] Implement guaranteed human handoff (§4.2)
- [ ] Enable automated retention enforcement (§4.3)
- [ ] Build subject-rights script (§4.4), at least a manual SOP if not automated
- [ ] Write DPIA for the deployment
- [ ] Write RoPA entry for the deployment
- [ ] Confirm operator has EU representative if needed (§3.5)
- [ ] Publish operator privacy notice with EU supplement
- [ ] Run breach-response tabletop exercise once before go-live
- [ ] Legal counsel sign-off
