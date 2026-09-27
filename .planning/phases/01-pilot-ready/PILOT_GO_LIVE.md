# All Green Pilot, Go-Live Checklist

Step-by-step checklist for activating the All Green Pest Control pilot.
Complete each step in order. Do not skip Twilio steps, the routing won't work without them.

---

## Part 1, Twilio Account Upgrade (TWIL-01)

The Twilio trial account cannot receive inbound calls from external numbers.

- [ ] Log in to console.twilio.com
- [ ] Go to **Account → Billing** and upgrade to a paid account
- [ ] Confirm "Upgrade complete" banner appears and trial restrictions are removed
- [ ] Record: Date upgraded: ____________  Operator: ____________

---

## Part 2, Provision All Green's Twilio Number (TWIL-02)

All Green needs its own inbound number, distinct from Clear Slope's number.

- [ ] In Twilio console → **Phone Numbers → Manage → Buy a Number**
- [ ] Select a local Vancouver area code (604 or 778) or All Green's preference
- [ ] Purchase the number and note the E.164 format (e.g. `+16041234567`)
- [ ] All Green's Twilio number: ____________________

**Configure the webhook:**

- [ ] Click the purchased number → **Voice Configuration**
- [ ] Set **"A call comes in"** → Webhook → `https://clearslope-receptionist.fly.dev/twilio/voice`
- [ ] HTTP method: **POST**
- [ ] Save

---

## Part 3, Update allgreen.json (TWIL-02)

- [ ] Edit `config/clients/allgreen.json`
- [ ] Replace `+1YYYYYYYYYY` with the actual number provisioned above
- [ ] Verify `minute_ceiling` is `200`
- [ ] Commit and deploy:
  ```
  git add config/clients/allgreen.json
  git commit -m "ops: set All Green Twilio number for go-live"
  fly deploy --app clearslope-receptionist
  ```

---

## Part 4, Set All Green Environment Secrets

All Green needs its own Google Sheet and SMS numbers. Use `fly secrets`, never paste secrets into files or chat.

```bash
fly secrets set --app clearslope-receptionist \
  ALLGREEN_GOOGLE_SHEET_ID=<all-green-sheet-id> \
  ALLGREEN_TWILIO_SMS_FROM=<all-green-twilio-number> \
  ALLGREEN_OWNER_NOTIFY_NUMBER=<soheil-cell-number>
```

- [ ] `ALLGREEN_GOOGLE_SHEET_ID` set (must be shared with the service account email)
- [ ] `ALLGREEN_TWILIO_SMS_FROM` set (can be the same number as the inbound number)
- [ ] `ALLGREEN_OWNER_NOTIFY_NUMBER` set (optional, owner emergency alerts)
- [ ] Confirm no bare `GOOGLE_SHEET_ID` leak: `fly secrets list | grep SHEET` should show only prefixed vars

---

## Part 5, Verify Routing (Success Criterion 1 & 2)

**Check the registry loaded both clients:**

```bash
fly logs --app clearslope-receptionist | grep "client registry ready"
# Expected: client registry ready: 2 client(s), ['clearslope', 'allgreen']

fly logs | grep "client=allgreen"
# Should see: client=allgreen prefix=ALLGREEN: prompt vX-...
```

**Verify routing with curl:**

```bash
# All Green's number → should return 200 TwiML
curl -s -o /dev/null -w "%{http_code}" \
  -d "To=<ALLGREEN_NUMBER>&From=+15550001234&CallSid=test_$(date +%s)" \
  -H "x-twilio-signature: skip" \  
  "https://clearslope-receptionist.fly.dev/twilio/voice"
# Note: sig validation will 403 in prod, use ALLOW_UNSIGNED_TWILIO=1 in staging only
```

**Healthz shows both clients:**

```bash
curl https://clearslope-receptionist.fly.dev/healthz | jq '.clients'
# Expected: ["+1XXXXXXXXXX", "+1YYYYYYYYYY"] (with real numbers)
```

- [ ] Registry log shows 2 clients
- [ ] All Green's Twilio number appears in `clients` list
- [ ] `clients_with_no_sheet` is empty (all sheets configured)

---

## Part 6, Verify Data Isolation (Success Criterion 2)

- [ ] Place a test call to All Green's number from a personal mobile
- [ ] Confirm the AI answers and books a dummy request
- [ ] Confirm booking appears in **All Green's Sheet** (not Clear Slope's)
- [ ] Confirm Clear Slope's Sheet is **unchanged**
- [ ] Check log: `fly logs | grep "client_id=allgreen"` shows the call

---

## Part 7, Verify Minute Ceiling (Success Criteria 3 & 4)

**Confirm minute counter is visible in logs:**

```bash
fly logs | grep "minutes_charged"
# Expected after a call: call_sid=... client_id=allgreen elapsed_sec=... minutes_charged=... total_minutes=...
```

**Extract per-client usage (grep recipe for COST-03):**

```bash
fly logs --app clearslope-receptionist | grep "minutes_charged" | grep "client_id=allgreen"
# Each line: total_minutes=N shows cumulative minute usage for All Green
```

- [ ] Minute log line visible after test call
- [ ] `minute_ceiling: 200` appears in `config/clients/allgreen.json`
- [ ] (Optional) Simulate ceiling: temporarily set `minute_ceiling: 1`, make a 2-minute call, confirm rejection TwiML

---

## Part 8, Verification of All 5 Phase 1 Success Criteria

| # | Criterion | Verification | Status |
|---|-----------|-------------|--------|
| 1 | `allgreen.json` exists, app loads both clients without touching Clear Slope config | Registry log shows 2 clients; Clear Slope config unchanged | ☐ |
| 2 | All Green calls write to separate Sheet; Clear Slope Sheet unaffected | Test call + Sheet inspection | ☐ |
| 3 | `minute_ceiling: 200` causes rejection past limit | Ceiling test above | ☐ |
| 4 | Per-client minute usage visible in structured logs | `grep minutes_charged` recipe above | ☐ |
| 5 | Twilio account off trial; All Green number provisioned + routed | Parts 1–2 above | ☐ |

---

## Sign-Off

All five criteria verified by:

- Date: ____________
- Operator: ____________
- All Green Twilio number: ____________________
- All Green Sheet ID: ____________________

Commit this filled-in checklist:
```bash
git add .planning/phases/01-pilot-ready/PILOT_GO_LIVE.md
git commit -m "ops(pilot): sign off All Green go-live checklist"
```
