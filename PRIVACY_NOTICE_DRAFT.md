# Privacy Notice, Client Deployment Template

Suggested clauses for an operator's public privacy policy when their phone line is handled by this AI receptionist. **Not legal advice, have a lawyer review before publishing.** This document captures what the system actually does so the lawyer doesn't have to reverse-engineer it.

## How to use this template

Each operator is a **data controller** for their callers' personal information. Clear Slope Digital, as the provider of this receptionist software, is a **data processor** acting under the operator's instructions (codified in the per-client config and the DPA you sign with us).

This document has two parts:

1. **Base clauses** (below), written for Canadian operators under PIPEDA and BC PIPA. Use these for any deployment.
2. **EU / UK / Swiss supplement** (at the bottom), additional clauses to add if the operator's callers are located in the EEA, UK, or Switzerland. GDPR / UK GDPR / Swiss FADP applies in that case, and the engineering preconditions in `docs/gdpr-gap-analysis.md` must be met before launch.

Markers in the text:
- `[OPERATOR_NAME]`, `[OPERATOR_CONTACT_EMAIL]`, `[OPERATOR_JURISDICTION]`, substitute before publishing.
- `(optional)`, clause is conditional on operator's specific situation.

Applies to: any client onboarded via `config/clients/<client>.json`. Current sandbox deployment is the Clear Slope Pest Control test instance, not a real client.

---

## What we collect when you call

When you call our main number, your call is initially handled by an AI receptionist (the assistant identifies itself as such on every call and discloses that the call may be recorded for quality and follow-up). During the call we collect:

- **Audio of your voice** during the call, transcribed to text by a third-party speech recognition service.
- **Your phone number** (caller ID), if your carrier provides it. We store only the last four digits in our intake records.
- **Information you provide voluntarily**: your name, service address, the issue you're calling about, your preferred callback time, and (when booking) a payment card.

We do **not** persistently store the raw audio after the call ends. The audio is transcribed in real time and discarded.

## How we use it

- **To take your service request** and pass it to a human team member for follow-up.
- **To send you a confirmation SMS** after your call so you know we received your request. The SMS is sent to the number you called from. Reply STOP at any time to opt out of further SMS.
- **For emergency triage**: if you describe an urgent situation (e.g., active wasps in a wall with an allergic resident in the home), we send a real-time SMS alert to the business owner so they can prioritize your call. This SMS contains your name, callback number, address, and a brief description.
- **For quality review**: transcripts of calls are automatically scrubbed of personal information (names, phone numbers, addresses) and may be reviewed by our team to improve how the AI handles future calls.

## Who we share it with

We use third-party service providers to operate the call line. They process your information solely on our behalf:

- **Twilio** (USA), handles the phone call routing and SMS delivery.
- **Deepgram** (USA), converts your speech to text in real time.
- **Anthropic** (USA), runs the AI assistant that decides what to say and what information to collect.
- **Cartesia** (USA), converts the AI's text replies back to speech so you can hear them.
- **Google Sheets** (USA), where we record the structured intake (your name, address, etc.) for our team to follow up.

We do not sell your information.

## How long we keep it

- **Booking intake records** (name, last-4 of phone, address, pest description, etc.), kept for as long as you remain a customer plus a reasonable period for tax/recordkeeping purposes (typically seven years), or until you ask us to delete them.
- **Call transcripts**, automatically scrubbed of personal information before any human review. Redacted transcripts are kept for up to 90 days for quality review, then deleted. Raw (un-scrubbed) transcripts are never persisted beyond the call.
- **Owner-alert SMS**, these sit in the owner's phone indefinitely (the owner needs to be able to call you back). They contain your full callback number.

## Your rights

You can ask us to:
- See what we have on file about you.
- Correct anything that's wrong.
- Delete your records (subject to legal recordkeeping requirements).

To make a request, contact us at [contact email / phone].

## Recording disclosure

Calls are transcribed by AI in real time. The transcript is automatically scrubbed of personal information and may be reviewed by our team to improve service. You can decline transcript retention at any time during the call by saying "no transcript", your service request will still be taken, but the transcript of the call will not be retained.

If you do not consent to any transcript handling, you can hang up and reach us via [alternative contact method, e.g., email or web form].

---

## Implementation notes (not for the public-facing policy)

- **Where this lives**: paste a customer-friendly version of the above into the operator's website privacy policy page. For Clear Slope, that's the privacy section of their website.
- **Per-client variation**: if a future client has different retention practices or a different complaint contact, edit before publishing, the snippet above is the union of plausibly-applicable clauses.
- **Recordkeeping**: ensure the privacy policy publication date is logged so you can show what notice was in effect at any given time.
- **CASL note for SMS**: the customer-confirmation SMS is exempt from CASL consent because it's a transactional reply to an inbound call, but the "Reply STOP to opt out" line is included for safety regardless.
- **PIPEDA / BC PIPA**: this draft is written with both in mind. A lawyer should confirm it meets the specific notification requirements of the operator's jurisdiction before publishing.
- **EU / UK / Swiss callers**: do not deploy this template as-is. The base clauses above are not sufficient for GDPR. Use the supplement below in addition, and confirm the engineering preconditions in `docs/gdpr-gap-analysis.md` are met before going live.

---

# EU / UK / Swiss Supplement

Use these additional clauses **on top of** the base notice above when the operator's callers are located in the European Economic Area, the United Kingdom, or Switzerland. They satisfy GDPR Articles 13/14, UK GDPR equivalents, and the Swiss FADP.

Engineering and contractual preconditions before deploying for EU callers, see `docs/gdpr-gap-analysis.md`. **Do not enable this supplement until those gaps are closed.**

## Identity and EU representative

- **Data controller**: [OPERATOR_LEGAL_NAME], registered at [OPERATOR_REGISTERED_ADDRESS].
- **Data processor**: Clear Slope Digital, Vancouver, BC, Canada (provides the AI receptionist software under a Data Processing Agreement with the controller).
- **EU representative (Article 27)**: [REPRESENTATIVE_NAME, ADDRESS, EMAIL]. Required if the controller has no EU establishment but offers services to EU residents. Leave out if the controller is itself established in the EU.
- **Data Protection Officer**: [DPO contact, if appointed, usually only required for large-scale special-category processing].

## Lawful basis for each processing purpose

| Processing | Lawful basis (GDPR Article 6) |
|---|---|
| Taking your service request and routing it to the team | 6(1)(b), steps prior to a contract |
| Sending you a confirmation SMS | 6(1)(b), performance of contract |
| Sending the owner a real-time SMS for urgent situations | 6(1)(d) vital interests, supported by 6(1)(f) legitimate interests |
| Reviewing call transcripts for quality assurance | 6(1)(f) legitimate interests, you may object |
| Recording the call (when consented) | 6(1)(a) explicit consent |
| Keeping booking records past the call | 6(1)(b) contract + 6(1)(c) legal obligation (tax recordkeeping) |

## Recording consent, explicit opt-in

The receptionist obtains your consent to record at the start of the call, before any service intake:

> "Hi, you've reached [OPERATOR_NAME]. This call may be recorded so we can serve you accurately. To agree, just keep talking. To decline, say 'no recording' and I'll take your request without recording. You can speak to a human at any time by saying 'human'."

Hanging up is not the only way to decline. Saying "no recording" disables recording for the call but does not end the call. You can withdraw consent at any time during the call by saying "stop recording," and you can withdraw consent for past recordings by emailing [OPERATOR_CONTACT_EMAIL].

## Special category data (Article 9)

Calls about pest control or similar service intake can incidentally surface health information (allergies, immunocompromised residents, presence of pregnant household members, etc.). This is special-category personal data under GDPR Article 9. We process it under:

- Article 9(2)(a), your explicit consent given for the call, **or**
- Article 9(2)(c), vital interests, where the situation is urgent (e.g., severe allergic reaction reported by the caller).

Special-category data is not retained beyond what is needed for the immediate response and is not used for analytics or model training.

## Sub-processors and international transfers

Your personal data is transferred to and processed in the United States by the providers listed below. Clear Slope Digital, our software provider, is located in Canada (which the European Commission recognizes as providing an adequate level of protection, Decision 2002/2/EC).

| Sub-processor | Role | Location | Transfer mechanism |
|---|---|---|---|
| Twilio | Call routing, SMS | United States | EU-U.S. Data Privacy Framework (certified) + SCCs |
| Deepgram | Real-time speech-to-text | United States | SCCs (Module 3) + Transfer Impact Assessment |
| Anthropic | AI language model | United States | SCCs (Module 3) + Transfer Impact Assessment |
| Cartesia | Real-time text-to-speech | United States | SCCs (Module 3) + Transfer Impact Assessment |
| Google (Sheets) | Booking-record storage | United States | EU-U.S. Data Privacy Framework (certified) + SCCs |
| Fly.io | Application hosting | Frankfurt (EEA) | Within EEA, no transfer mechanism needed |
| Clear Slope Digital | Software provider, processor | Canada | Adequacy decision (Commission Decision 2002/2/EC) |

A current sub-processor list is at [OPERATOR_WEBSITE]/privacy/processors. We notify you of changes at least 30 days before a new sub-processor begins processing your data, giving you the opportunity to object.

## Your GDPR rights

You have the right to:

- **Access**, confirmation that we process your data, and a copy of it.
- **Rectification**, correction of inaccurate data.
- **Erasure**, deletion, subject to legal recordkeeping obligations (typically tax records for 6 years).
- **Restriction**, pause processing in specific circumstances.
- **Portability**, your data in a machine-readable format, where the processing is based on consent or contract and carried out by automated means.
- **Object**, to processing based on legitimate interests, including quality-review transcript analysis.
- **Withdraw consent**, at any time, without affecting the lawfulness of processing before withdrawal.
- **Not be subject to a decision based solely on automated processing** that produces legal effects or similarly significantly affects you (Article 22). A human reviews every booking, and you can speak to a human at any point during the call by saying "human."

To exercise any of these rights, email [OPERATOR_CONTACT_EMAIL]. We will respond within one month. We may extend this by up to two further months for complex requests, in which case we will tell you.

You also have the right to lodge a complaint with your local supervisory authority. A directory is at [edpb.europa.eu](https://edpb.europa.eu/about-edpb/about-edpb/members_en).

## Automated decision-making (Article 22)

The AI receptionist categorizes calls (e.g., emergency vs. standard, residential vs. commercial) to route them appropriately. This is not "solely automated decision-making producing legal or similarly significant effects" within the meaning of Article 22(1), because:

- A human reviews every booking before service is dispatched.
- You can at any point ask to speak to a human, who will handle your call without AI involvement.
- The AI's categorization does not by itself deny anyone service or alter the terms on which service is offered.

## Retention, EU specifics

Retention periods stated in the base notice apply, with these additions:

- **Recorded audio (where consented)**: deleted within 30 days after the call.
- **Demo / quality-review transcripts**: deleted within 90 days, with personal identifiers redacted before any analysis if retention extends past that.
- **Tax recordkeeping (Article 6(1)(c))**: 6 years from the end of the tax year in which the service was provided (Member State law may vary).

## Children

This service is intended for adult callers contacting a service business. We do not knowingly process the personal data of anyone under 16. If we discover that we have collected personal data from a person under 16, we will delete it.

## Data breach notification

In the event of a personal data breach likely to result in a risk to your rights and freedoms, we will notify our supervisory authority within 72 hours of becoming aware of it, and notify you directly without undue delay where the breach is likely to result in a high risk to your rights and freedoms.
