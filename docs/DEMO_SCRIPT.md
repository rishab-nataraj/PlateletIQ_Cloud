# Demo script (about 12 minutes)

Two browser windows side by side — one normal, one private — plus your phone.
Before you start, open the API's `/api/v1/health` once so Render's free instance
is awake.

| Window | Signs in as | Facility |
|---|---|---|
| **A** (left) | your Gmail | RGGGH Chennai — the hospital *with* stock |
| **B** (right) | a second account | Apollo Greams Road — the hospital that is *short* |

Keep these tabs open in a third window: Supabase table editor, QStash console,
Discord channel, Sentry, Better Stack, DynamoDB "Explore items", UptimeRobot status page.

---

### 1. The platform at a glance (2 min) — window A

**Cloud services** screen.
- The *How a request moves* diagram: every box is coloured by live status, computed
  by the API just now.
- Point at the counts: *N live*. Scroll through the categories; each card says what the
  service does here, its free tier, and the AWS service that would do the same job.
- Press **Test** on Supabase PostgreSQL, Upstash Redis and Amazon IAM: real round trips,
  with timings.

### 2. Phone alerts in 30 seconds (1 min) — window A + phone

**Notifications → Link a Telegram chat** → scan the QR with the phone → **Start**.
The bot replies *Linked to RGGGH Chennai*. Type `/stock` in Telegram: live inventory
from Supabase.

### 3. A shortage, end to end (4 min)

1. **B → Transfers → Request units** → RGGGH → 12 SDP → priority **EMERGENCY** → send.
2. Within a second, **A** shows a red toast *Action needed* (Pusher; the top bar says
   *Live · Pusher*). At the same time:
   - the phone buzzes (Telegram),
   - Discord's ops channel shows the request,
   - the Gmail inbox gets the alert,
   - because it is an EMERGENCY, the AWS alerts topic emails the compliance address.
3. **A → Notifications**: the request's delivery chips — *✓ Email via Gmail API,
   ✓ Telegram, ✓ Discord ops, ✓ Live push* and *via QStash queue*. Open the QStash
   console → Logs: the message, its signed callback, its response.
4. **A** → **Authorise and book courier** → pickup code → confirm it → receipt code.
   **B** gets *Authorised* and then *On the way, ~33 min* without refreshing.
5. **B → Track shipment**: route from OSRM, cached in Upstash Redis.
6. Read A's receipt code aloud; **B** enters it. Both get *Delivered and settled* and a
   **Custody certificate** button — the PDF is stored in Amazon S3 and opened through a
   short-lived presigned link.

### 4. The record nobody can quietly edit (2 min) — AWS console

- **DynamoDB → plateletiq-custody-ledger → Explore items**: `TRANSFER#TRF-…`,
  `EVENT#000001…`, each `prev_hash` equal to the previous `hash`.
- **Lambda → plateletiq-compliance-report → Test**: the report email arrives —
  *"Hash-chain integrity: all chains verified"*.
- Optional: edit one item's `payload` in DynamoDB, run the report again → it names the
  tampered transfer.
- **CloudWatch → Alarms**: DLQ and Lambda-error alarms; **Budgets**: US$1 guardrail.

### 5. Things go wrong (2 min)

- **Sentry**: Cloud services → Sentry → **Test** → the issue appears in Sentry with a stack trace.
- **Failover**: on Render, temporarily break `GMAIL_REFRESH_TOKEN` → **Send test notification**
  → the email chip reads *✓ Email · Brevo (failover)*; hover to see why Gmail failed.
- **Logs**: Better Stack → search `notification delivered`.
- **Uptime**: the UptimeRobot status page; the Cloud services card shows when it last checked.

### 6. Scheduled work (1 min)

Cloud services → *Scheduled jobs* → **daily-digest → Run now for my facility**:
the digest email (stock table, transfers in progress, 7-day forecast) and Telegram
summary arrive. QStash **Schedules** shows the same job firing every morning at 08:00 IST,
and the heartbeat in Better Stack turns green.

---

**If something is not configured**, its card says *Not configured* and the demo still
works through the fallbacks (polling instead of Pusher, inline delivery instead of
QStash, on-demand PDF instead of S3). Say so plainly — the screen does.
