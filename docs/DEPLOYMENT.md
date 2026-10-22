# Deploying PlateletIQ to the cloud (all free)

Every step is independent: do them in order, and after each one open the console's
**Cloud services** screen — the service you just configured turns **Live**. Skip any
you do not need; the app keeps working without it.

You need a GitHub account and a Google account. Only step 14 (AWS) asks for a card,
and it is optional.

Where each variable goes:

| Variable prefix | Set it on |
|---|---|
| everything in `backend/.env.example` | **Render** (the API) — or Doppler project `plateletiq-api` |
| `VITE_*` (`frontend/.env.example`) | **Vercel** (the console) — or Doppler project `plateletiq-web` |

After changing Render variables, Render redeploys automatically. After changing
Vercel variables, redeploy the latest deployment so the build picks them up.

---

## 1. Supabase — database and file storage

1. [supabase.com](https://supabase.com) → **New project**. Region **Southeast Asia (Singapore)**
   (next to the Render region). Save the database password.
2. **Connect** (top bar) → **Session pooler** → copy the URI and put your password in it:
   `DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres`
3. **Project Settings → API**: copy the Project URL and the `service_role` key:
   `SUPABASE_URL=https://<ref>.supabase.co`, `SUPABASE_SERVICE_ROLE_KEY=...`
   (server-side only — never put it in Vercel). The bucket is created on first upload.

Tables are created automatically when the API starts.
Free projects pause after a week without activity — open the dashboard and click
*Restore* if that happens before a demo.

## 2. Render — the API

1. [render.com](https://render.com) → sign in with GitHub → **New → Blueprint** → choose this repository.
   Render reads `render.yaml`, creates **plateletiq-api** and generates `JWT_SECRET`,
   `OTP_SECRET` and `JOBS_SECRET`.
2. Fill in the prompted values: `DATABASE_URL` (step 1). For `FRONTEND_URL` and
   `ALLOWED_ORIGINS` put a placeholder now and fix them after step 3. Google values
   can wait for step 4.
3. For a demo without Google sign-in: service → **Environment** → `ALLOW_DEV_SIGNIN=1`.
4. Add `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`.
5. Open `https://plateletiq-api.onrender.com/api/v1/health` — `"status": "ok"`.

## 3. Vercel — the console

1. [vercel.com](https://vercel.com) → **Add New → Project** → import the repository.
2. **Root Directory: `frontend`** (framework Vite is detected).
3. Environment variable `VITE_API_BASE_URL=https://plateletiq-api.onrender.com/api/v1` → **Deploy**.
4. Project → **Analytics → Enable**, and **Speed Insights → Enable**.
5. Back on Render: `FRONTEND_URL=https://<project>.vercel.app` and
   `ALLOWED_ORIGINS=https://<project>.vercel.app`.

## 4. Google sign-in

Google Cloud Console → APIs & Services → Credentials → your OAuth client (Web):
- Authorised JavaScript origin: `https://<project>.vercel.app`
- Authorised redirect URI: `https://plateletiq-api.onrender.com/api/v1/auth/google/callback`

On Render set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` (that URI).

## 5. Upstash — Redis and QStash

1. [console.upstash.com](https://console.upstash.com) → **Redis → Create database**
   (region nearest Singapore/Mumbai) → **REST API** section →
   `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`.
2. **QStash** tab → copy `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY`,
   `QSTASH_NEXT_SIGNING_KEY` (and `QSTASH_URL` if a regional one is shown).
   Add all to Render. `PUBLIC_API_URL` is detected on Render automatically.
3. Create the three cron schedules, once, from your computer:

   ```bash
   cd backend
   pip install -r requirements.txt
   QSTASH_TOKEN=... PUBLIC_API_URL=https://plateletiq-api.onrender.com \
     python ../scripts/setup_qstash_schedules.py
   ```

   They appear in the QStash console under **Schedules**, and on the Cloud services screen.

## 6. Pusher — live updates

[dashboard.pusher.com](https://dashboard.pusher.com) → **Channels → Create app**, cluster
**ap2 (Mumbai)** → **App Keys** → `PUSHER_APP_ID`, `PUSHER_KEY`, `PUSHER_SECRET`,
`PUSHER_CLUSTER=ap2` on Render. The console's top bar changes from *Polling* to
*Live · Pusher*.

## 7. Email

Configure at least one. Order of use: `EMAIL_PROVIDERS=gmail,brevo,resend,ses`.

**Gmail API (recommended — reaches the inbox without owning a domain)**
1. Google Cloud Console (same project) → **APIs & Services → Library → Gmail API → Enable**.
2. **OAuth consent screen**: add your Gmail as a test user (or publish the app);
   scope `https://www.googleapis.com/auth/gmail.send`.
3. **Credentials → Create OAuth client ID → Desktop app**.
4. On your computer: `python scripts/gmail_refresh_token.py <client-id> <client-secret>`
   → sign in → copy the four printed `GMAIL_*` lines to Render.
   While the consent screen is in *Testing*, the token lasts 7 days — re-run before a demo.

**Brevo** — [app.brevo.com](https://app.brevo.com) → Senders: add and verify your
address → SMTP & API → API keys → `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`.
Without your own domain, check the spam folder.

**Resend** — [resend.com/api-keys](https://resend.com/api-keys) → `RESEND_API_KEY`.
Without a verified domain it only delivers to your own account address.

Who receives email: everyone who has signed in at that facility, plus addresses
added under **Notifications → Channels**.

## 8. Telegram

1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. On Render: `TELEGRAM_BOT_TOKEN=...`, `TELEGRAM_BOT_USERNAME=<bot name without @>`,
   `TELEGRAM_WEBHOOK_SECRET=<any long random string>`.
3. Once deployed:
   ```bash
   cd backend
   TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=... PUBLIC_API_URL=https://plateletiq-api.onrender.com \
     python ../scripts/setup_telegram_webhook.py
   ```
4. Console → **Notifications → Link a Telegram chat** → scan the QR → **Start**.

Locally (no public URL), leave `TELEGRAM_WEBHOOK_SECRET` empty and run the script
with `--delete`; the console polls Telegram instead.

## 9. Discord

Discord server → **Server Settings → Integrations → Webhooks → New Webhook** →
choose a channel such as `#plateletiq-ops` → **Copy Webhook URL** →
`DISCORD_WEBHOOK_URL` on Render.

## 10. Sentry

[sentry.io](https://sentry.io) → create two projects: **FastAPI** and **React**.
- FastAPI DSN → Render `SENTRY_DSN`
- React DSN → Vercel `VITE_SENTRY_DSN` (public by design), then redeploy

## 11. Better Stack — logs and heartbeats

1. [betterstack.com](https://betterstack.com) → **Telemetry → Sources → Connect source → Python** →
   `BETTERSTACK_SOURCE_TOKEN` and `BETTERSTACK_INGESTING_HOST` on Render.
2. **Uptime → Heartbeats → Create** three heartbeats: hourly (expiry sweep, 15 min grace),
   daily (digest, 1 h grace), daily (forecast refresh, 1 h grace). Copy the URLs to
   `HEARTBEAT_URL_EXPIRY_SWEEP`, `HEARTBEAT_URL_DAILY_DIGEST`, `HEARTBEAT_URL_FORECAST_REFRESH`.
   (Healthchecks.io works the same way.)

## 12. UptimeRobot

[uptimerobot.com](https://uptimerobot.com) → **New monitor → HTTP(s)** →
`https://plateletiq-api.onrender.com/api/v1/health`, every 5 minutes, alert to your email.
Add a second for the Vercel URL. **Status pages → Create** → add both monitors → copy
its URL to Render `STATUS_PAGE_URL` (the Cloud services screen links to it).

## 13. Doppler — secrets in one place (optional but recommended)

```bash
# install the CLI: https://docs.doppler.com/docs/install-cli
doppler login
doppler setup                     # uses doppler.yaml at the repo root
doppler secrets upload backend/.env --project plateletiq-api --config dev
cd backend && doppler run -- uvicorn main:app --port 8000
```

Dashboard → `plateletiq-api` → **prd** config → **Integrations → Render** (sync to
plateletiq-api); `plateletiq-web` → **prd** → **Integrations → Vercel**. From then on,
change secrets in Doppler only.

## 14. AWS (optional, needs a card for sign-up; stays inside the free tier)

Follow [aws/README.md](../aws/README.md): upload `aws/plateletiq-aws.yaml` to
CloudFormation in **ap-south-1**, confirm the SNS email, create an access key for
the `plateletiq-backend` user, and copy the stack outputs to Render.

## 15. GitHub Actions

CI runs on every push without setup. For the on-demand job runner
(**Actions → Run a job**): repository **Settings → Secrets and variables → Actions** →
secret `JOBS_SECRET` (copy it from Render's environment) and variable
`API_URL=https://plateletiq-api.onrender.com`.

---

## Check everything

Sign in → **Cloud services**. Each configured service shows **Live**; press **Test**
on each. Then **Notifications → Send test notification** and watch the delivery
chips fill in (email, Telegram, Discord, live push).

Command-line equivalent: `curl https://plateletiq-api.onrender.com/api/v1/health`
lists every integration's mode.
