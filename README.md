# PlateletIQ

**Platelets expire in five days. One hospital discards them the same week another turns a patient away.**

PlateletIQ forecasts platelet demand per hospital, and when a shortage is coming, moves units from a facility that has them — with custody verified by one-time code at both ends, a booked courier, and a live map.

> DSU DevHack 3.0 · Healthcare + AI/ML · extended for Cloud Architecture Design
> 20 eRaktKosh-registered blood centres across Chennai
> **35 managed cloud services, all on free tiers** — see [docs/CLOUD_SERVICES.md](docs/CLOUD_SERVICES.md)

---

## The problem

A platelet concentrate has a **five-day shelf life** and must be held at 20–24 °C under constant agitation. It cannot be frozen, cannot be stockpiled, and cannot be produced on demand — a single-donor apheresis unit takes about 90 minutes of a donor's time.

That creates a problem no individual hospital can solve alone:

- A tertiary centre finishing a week under its forecast **discards** usable units.
- A hospital 3 km away, hit by an unexpected dengue admission, **has none**.
- Neither can see the other's stock, and there is no operational path to move a unit between them even when both would benefit.

eRaktKosh publishes national stock data, but as a **directory, not a workflow**. It tells you a facility reported 14 SDP units at some point. It does not tell you whether they are still usable, it cannot reserve them, and it cannot get a box across a city with custody intact.

PlateletIQ is the missing workflow.

---

## What it does

**Forecasts demand** — a LASSO quantile model produces a 7-day forecast (q50/q67/q90) per facility, and a deterministic decision engine turns that into one of three actions: `HOLD`, `PROCURE`, or `COLLECT`, each with its evidence chain.

**Finds the counterparty** — ranks every other registered facility by road distance and by what they *currently* hold, so a request goes to somebody who can actually fill it.

**Moves the units** — a transfer reserves real inventory rows nearest-expiry-first, books a courier, and settles onto the receiving facility's ledger only when the receiver redeems the sender's code.

**Tracks the shipment** — road-routed map, courier marker, and a scan-by-scan timeline, visible to both facilities from one source of truth.

---

## Cloud architecture

The console and API run as separate cloud services, and everything around a
transfer — telling the right people, keeping an independent record, watching for
failure — runs on managed services, each on a free tier.

| Layer | Services |
|---|---|
| Hosting | **Vercel** (console + Web Analytics + Speed Insights), **Render** (API, `render.yaml` blueprint) |
| Data | **Supabase** PostgreSQL + Storage, **Upstash Redis** |
| Messaging & schedules | **Upstash QStash** (queue with retries + dead-letter queue, cron), **Pusher Channels** (live updates) |
| Notifications | Email via **Gmail API → Brevo → Resend → Amazon SES** (failover), **Telegram** bot, **Discord** ops channel, in-app inbox |
| Compliance (AWS Free Tier) | **SNS → SQS (+DLQ) → Lambda → DynamoDB** hash-chained custody ledger, **EventBridge Scheduler** daily report, **S3** custody certificates, **IAM**, **CloudWatch**, **CloudFormation**, **Budgets** |
| Observability | **Sentry**, **Better Stack** logs + heartbeats, **UptimeRobot** + status page |
| Security & delivery | **Google Identity**, **Doppler** secrets, **GitHub Actions** CI, **Docker** |

Every integration is switched on by environment variables and degrades
gracefully when absent. The console's **Cloud services** screen shows each one's
live status — computed by the running API, with a **Test** button — and the
**Notifications** screen shows exactly how every alert was delivered.

- [docs/CLOUD_SERVICES.md](docs/CLOUD_SERVICES.md) — every service: role, free tier, AWS equivalent, where in the code
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — creating the free accounts, step by step
- [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) — a 12-minute walkthrough
- [aws/README.md](aws/README.md) — the AWS stack, deployable from one CloudFormation file

---

## The part that matters: verified custody

Blood products are a controlled, traceable medical product. "Trust the courier scan" is not an acceptable chain of custody. So the handshake is enforced server-side, not decorated in the UI.

Every transfer row names a **sender** and a **receiver**. Who opened it depends on direction:

| Direction | Opened by | Sender | Receiver |
|---|---|---|---|
| `SHORTAGE_PULL` | the facility that is short | the counterparty | the opener |
| `WASTAGE_PUSH` | the facility holding near-expiry units | the opener | the counterparty |

Either way, **the sender authorises and issues both codes; the receiver redeems the second one.**

```
REQUESTED
   │  sender authorises: reserves stock, books courier, issues pickup code
   ▼
UNITS_RESERVED ──► SHIPMENT_CREATED ──► AWB_ASSIGNED ──► PICKUP_OTP_REQUIRED
                                                                │
                                          sender confirms pickup code
                                                                ▼
                                                          IN_TRANSIT
                                                                │
                                        courier scan (optional) ▼
                                                             ARRIVED
                                                                │
                                   receiver redeems receipt code
                                                                ▼
                                                     TRANSFER_COMPLETED
```

What the server **refuses**, with a 403 rather than a silent no-op:

- the receiver cannot authorise its own request
- the sender cannot redeem the receipt code it issued
- neither console can read a code issued to the other
- a courier webhook reading `DELIVERED` moves the row to `ARRIVED` and **cannot** settle stock

Codes are 6 digits from a CSPRNG, stored only as HMAC-SHA256 digests, returned exactly once to the issuer, single-use, expiring, attempt-capped, and rate-limited. A wrong-facility attempt is rejected *without* burning an attempt, so one console cannot lock another out.

Full detail: **[docs/TRANSFERS.md](docs/TRANSFERS.md)**

---

## Inventory actually moves

Not a counter — real `InventoryUnit` rows.

- **Authorise** reserves units **nearest expiry first**, so a transfer drains exactly the stock that would otherwise be wasted. Insufficient stock returns 409 and changes nothing.
- **Courier dispatch failure** rolls the reservation back before returning 502, so a logistics outage never strands usable units.
- **Receipt** re-parents those exact bags to the receiver. The bag keeps its id, bag number and expiry, so provenance survives the move.

Every step writes an `InventoryEvent` on both sides plus an `AuditLog` entry.

---

## Try it in two minutes

**Windows** — double-click `start.bat`. **macOS / Linux** — `./start.sh`.

It creates the virtual environment, installs both dependency sets, generates secrets, starts the backend, **waits until the API actually answers**, then starts the frontend and opens the browser. Re-running skips whatever is already in place.

### The two-hospital demo

One backend, two browser sessions signed in at **different facilities**. An incognito window is enough; two laptops on a LAN work identically.

| # | Where | What happens |
|---|---|---|
| 1 | Both | Sign in, choose a facility — **A → RGGGH Chennai**, **B → Apollo Greams Road** |
| 2 | B | **Transfers → Request units** → pick RGGGH → 12 SDP units → send |
| 3 | A | Appears under *Needs your action* in ~2.5s. No refresh — both consoles poll one backend |
| 4 | A | **Authorise and book courier** → stock reserves, courier books, **A's pickup code** appears |
| 5 | A | Enter that code → **In transit**, and **A's receipt code for B** appears |
| 6 | Both | **Track shipment** — live map, moving marker, shared timeline |
| 7 | A → B | Pass the receipt code across, as you would by phone in practice |
| 8 | B | Enter it → 12 units leave A's ledger and land on B's |

Worth trying: have B attempt step 4, or A attempt step 8. Both get a plain 403.

---

## Architecture

```
  Vercel                      Render                          External
  React 19 + Vite             FastAPI + SQLAlchemy
  ───────────────             ────────────────────            ────────
  Facility picker  ─────►     /auth/select-facility   ◄────   Google Identity
  Transfers        ─────►     /transfers/*            ─────►  Shiprocket (courier)
  Live tracking    ─────►     /transfers/{id}/track   ─────►  Mapbox │ OSRM (routing, cached in Upstash Redis)
  Forecast         ─────►     /forecast/*             ─────►  Supabase Storage (uploaded history)
  Notifications    ─────►     /notifications/*                LASSO model (joblib)
  Cloud services   ─────►     /system/*  /jobs/*              Supabase PostgreSQL
        ▲                          │   ▲
        │ Pusher (private channel) │   │ QStash: queued delivery + cron (signed)
        └──────────────────────────┤   └───────────────
                                   ├──► Email (Gmail API → Brevo → Resend → SES) · Telegram · Discord
                                   ├──► AWS: SNS → SQS → Lambda → DynamoDB ledger · S3 certificates
                                   └──► Sentry · Better Stack · (UptimeRobot watches /health)
                             /webhooks/shiprocket ◄─────   courier scans
                             /webhooks/telegram   ◄─────   bot messages
```

**Auth is two-stage.** Google establishes identity; the JWT carries **no facility**. A second screen binds the session to a registered facility and re-issues the token with `bank_id`. Every stock-touching endpoint reads the facility off the token — so two browsers running one build are two different hospitals purely because each chose differently. There is no ambient default facility; an unbound session gets a 403 telling it to choose.

**Facility registry.** 20 Chennai blood centres with eRaktKosh codes, addresses, pincodes and coordinates, seeded on first start with opening stock spread realistically across the 5-day shelf life. Coordinates resolve through OpenStreetMap Nominatim when `GEOCODE_FACILITIES=1`, otherwise from the curated registry.

---

## What is real, and what is not

Judges deserve this plainly.

| Component | Status |
|---|---|
| Transfer pipeline, role enforcement, OTP crypto | **Real.** Fully implemented and server-enforced. |
| Inventory reservation and settlement | **Real.** Actual rows move between facilities. |
| Road routing and ETA | **Real.** Mapbox with a token, otherwise public OSRM. Falls back to a straight-line estimate and **says so in the UI** when both are unreachable. |
| Map | **Real.** Leaflet on OpenStreetMap, no key required. |
| Shiprocket courier | **Adapter is real** (auth, serviceability, adhoc order, AWB, pickup, tracking, webhooks). Without credentials it runs simulated, and the UI and `/health` both say `simulated`. |
| Courier GPS position | **Honest.** With no live feed, the marker is projected along the routed polyline from elapsed time and labelled *"Projected along the route from elapsed time — not a GPS fix"*. The simulated adapters return **no** coordinates rather than inventing one. |
| Facility coordinates | Curated from public addresses. eRaktKosh does not publish lat/long; optional Nominatim geocoding is built in. |
| Demand model | Trained on the Aachen hospital dataset (2008–2018, 4,018 days), calibrated with a WHO India monthly index. It is **not** trained on Chennai data — the adapter reports its own limitations through `/analytics/model-health`. |
| Opening inventory | Seeded, deterministic per facility. Real operation would ingest from the facility's LIS. |
| Cloud integrations | **Real when configured.** Each is switched on by environment variables; the Cloud services screen shows which are live right now, and its Test buttons make real round trips. Unconfigured ones say *Not configured* and a fallback runs. |
| Notifications | **Real.** Delivered per channel with status, provider and error recorded; never contain custody codes. |

`GET /api/v1/health` reports which integrations are genuinely configured, so nothing has to be taken on trust.

---

## Model performance

| Metric | PlateletIQ | Naive baseline | Schilling et al. |
|---|---|---|---|
| MASE | **0.734** | 0.993 | 0.746 |
| Wastage rate | **3.25 %** | 9.61 % | — |

Measured on the training dataset's holdout, not on Chennai operations. The adapter ships its own `known_limitations` and exposes them through the API rather than hiding them behind a headline number.

---

## Tech stack

**Frontend** — React 19, TypeScript, Vite 8, Tailwind 4, TanStack Query 5, Recharts, Leaflet, pusher-js, Sentry
**Backend** — Python, FastAPI, Pydantic v2, SQLAlchemy 2, Uvicorn, psycopg 3, boto3
**ML** — scikit-learn LASSO quantile pipeline
**Logistics** — Shiprocket API; Mapbox Directions or OSRM
**Auth** — Google Identity Services → facility binding, HS256 JWT
**Data** — SQLite locally, Supabase PostgreSQL in the cloud
**Cloud** — Vercel, Render, Supabase, Upstash (Redis, QStash), Pusher, Gmail API / Brevo / Resend, Telegram, Discord, Sentry, Better Stack, UptimeRobot, Doppler, GitHub Actions, Docker, AWS (SNS, SQS, Lambda, DynamoDB, EventBridge Scheduler, S3, SES, IAM, CloudWatch, CloudFormation, Budgets)

---

## Configuration

`backend/.env.example` documents every setting, including every cloud
integration (all optional — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)). The core ones:

| Variable | Why |
|---|---|
| `JWT_SECRET`, `OTP_SECRET` | **Must be set and stable.** Otherwise every session and live OTP dies on restart. |
| `ALLOW_DEV_SIGNIN` | Passwordless local sign-in for testing. **Leave off anywhere real** — anyone reaching the port could sign in as any facility. |
| `GOOGLE_CLIENT_ID` / `_SECRET` | Google sign-in. ID tokens are verified against the `aud` claim. |
| `TRANSPORT_MODE=live` + `SHIPROCKET_*` | Book real shipments instead of simulating. |
| `SHIPROCKET_WEBHOOK_TOKEN` | Webhook intake is **refused entirely** until set. |
| `MAPBOX_ACCESS_TOKEN` | Traffic-aware routing; OSRM is used without it. |
| `ALLOWED_ORIGINS` | Add the LAN address to run the two consoles on two machines. |
| `DATABASE_URL` | Supabase session-pooler URI in the cloud; SQLite locally. |
| `JOBS_SECRET` | Lets GitHub Actions / curl trigger scheduled jobs. |

---

## Known limitations

- Opening inventory is seeded; there is no LIS ingestion yet.
- Without Pusher configured, consoles poll every 2.5s; with it, changes are pushed and polling is a 20s safety net.
- The forecast model is trained on German data; Indian seasonality is applied as a calibration index, not learned.
- One backend serves both facilities. A real deployment would federate per-facility instances behind a registry.
- Cold-chain temperature is an instruction to the courier, not telemetry. No IoT sensor is integrated, and the UI does not claim one.

---

## Repository

```
backend/
  cloud/          one adapter per cloud service: email · telegram · discord · realtime (Pusher)
                  qstash · cache (Upstash Redis) · storage (Supabase) · aws · observability · registry
  routers/        auth · facilities · transfers · webhooks · forecast · inventory
                  notifications · jobs · system · realtime
  services/       otp · inventory_ledger · routing · courier_tracking · facility_registry
                  notifications · transfer_notifications · jobs · custody_stream · certificate_service
  services/transport/   shiprocket · porter · beckn · internal (one interface)
  models/         SQLAlchemy schema (+ notifications, deliveries, telegram links, job runs)
  tests/          pytest: notification pipeline, signatures, jobs, realtime auth, Telegram, ledger stream
frontend/src/
  screens/        Login · FacilitySelect · Transfers · TransferTracking · Notifications · CloudServices
  contexts/       AuthContext · LiveContext (Pusher, toasts, browser notifications)
  components/     RouteMap (Leaflet) · OtpPanels · LiveWidgets
  api/            typed client, dynamic facility scoping
aws/              CloudFormation stack, Lambda sources, moto tests
docs/             TRANSFERS · CLOUD_SERVICES · DEPLOYMENT · DEMO_SCRIPT
scripts/          QStash schedules · Telegram webhook · Gmail token · env bootstrap
render.yaml       Render blueprint          doppler.yaml   Doppler secrets
docker-compose.yml  Postgres + API + console  .github/workflows/  CI · run a job
start.bat / start.sh
```

---

*Built for DSU DevHack 3.0. Facility data from the eRaktKosh national blood bank registry, Ministry of Health & Family Welfare.*
