"""
The catalogue behind ``GET /api/v1/system/integrations`` and the Cloud
services screen.

Every entry says what the service does *in this project*, whether it is live
right now (computed, never asserted), its free-tier allowance, and the AWS
service that would play the same role — the same columns as the course's
reference projects, but checked at runtime instead of typed into a report.

Status vocabulary:
    live      configured and in use
    fallback  not configured; a built-in substitute is running (said plainly)
    off       not configured; the feature is unavailable
    build     part of the build/deploy pipeline — not observable from a running API
    frontend  runs in the browser; the console reports it from its own build
"""

import os
import time
from typing import List

from cloud import aws, cache, discord, qstash, realtime, storage, telegram
from cloud import email as email_service
from cloud.config import env
from cloud.observability import betterstack_status, sentry_status

JOB_NAMES = ["expiry-sweep", "daily-digest", "forecast-refresh"]


def _monitor_seen(name: str) -> str:
    seen = cache.get_json(f"monitor:{name}:last")
    if not seen:
        return "no check received yet"
    minutes = int((time.time() - float(seen)) // 60)
    return "last check just now" if minutes < 1 else f"last check {minutes} min ago"


def _database() -> dict:
    url = env("DATABASE_URL", "sqlite:///./plateletiq.db")
    if url.startswith("postgres"):
        host = url.split("@")[-1].split("/")[0].split(":")[0]
        supabase = "supabase" in host
        return {
            "status": "live",
            "detail": f"PostgreSQL at {host}" + ("" if supabase else " (not a Supabase host)"),
        }
    return {
        "status": "fallback",
        "detail": "SQLite file on local disk. Fine on a laptop; on Render's free tier it is wiped on every restart.",
    }


def catalogue() -> List[dict]:
    email_providers = email_service.status()["providers"]
    routing_mapbox = bool(env("MAPBOX_ACCESS_TOKEN"))
    courier_live = env("TRANSPORT_MODE", "mock").lower() == "live" and bool(env("SHIPROCKET_EMAIL"))
    stack_deployed = aws.sns_enabled()
    db = _database()
    beats = {job: bool(env(f"HEARTBEAT_URL_{job.upper().replace('-', '_')}")) for job in JOB_NAMES}
    doppler = env("DOPPLER_PROJECT") or env("DOPPLER_CONFIG")

    def item(key, name, category, role, status, detail, free_tier, aws_equivalent, test=False, env_vars=(), dashboard=None):
        return {
            "key": key,
            "name": name,
            "category": category,
            "role": role,
            "status": status,
            "detail": detail,
            "free_tier": free_tier,
            "aws_equivalent": aws_equivalent,
            "testable": test,
            "env": list(env_vars),
            "dashboard": dashboard,
        }

    def stack(key, name, role, detail_live, free_tier, test=False, env_vars=()):
        return item(
            key, name, "AWS compliance stack", role,
            "live" if stack_deployed else "off",
            detail_live if stack_deployed else "Deploy aws/plateletiq-aws.yaml, then set the stack outputs in the API environment.",
            free_tier, "— (this is the AWS service)", test, env_vars, "https://console.aws.amazon.com/",
        )

    return [
        # ── Hosting & delivery ──────────────────────────────────────────────
        item("vercel", "Vercel", "Hosting & delivery",
             "Builds the React console on every push and serves it from a global edge network with HTTPS.",
             "frontend", "Reported by the console from its own build.",
             "Hobby: unlimited static sites, 100 GB bandwidth/month",
             "AWS Amplify Hosting (or S3 + CloudFront)", dashboard="https://vercel.com/dashboard"),
        item("vercel_analytics", "Vercel Web Analytics + Speed Insights", "Hosting & delivery",
             "Privacy-friendly page views and real-user Core Web Vitals for the console.",
             "frontend", "Enabled from the Vercel project's Analytics and Speed Insights tabs.",
             "Hobby: 50k analytics events/month", "Amazon CloudWatch RUM", dashboard="https://vercel.com/dashboard"),
        item("render", "Render", "Hosting & delivery",
             "Runs the FastAPI backend as a web service, deployed from GitHub, with health checks on /api/v1/health.",
             "live" if env("RENDER") else "off",
             f"Running on Render ({env('RENDER_SERVICE_NAME') or 'web service'})" if env("RENDER") else "Not running on Render (local or another host).",
             "Free instance: 750 h/month, sleeps after 15 min idle", "AWS App Runner / Elastic Beanstalk",
             dashboard="https://dashboard.render.com/"),
        item("render_blueprint", "Render Blueprint (render.yaml)", "Hosting & delivery",
             "Infrastructure as code for the backend service, its health check and every environment variable.",
             "build", "render.yaml at the repository root.", "Free", "AWS CloudFormation"),

        # ── Data & storage ──────────────────────────────────────────────────
        item("supabase_postgres", "Supabase PostgreSQL", "Data & storage",
             "System of record: facilities, inventory ledger, transfers, custody codes (as HMAC digests), notifications.",
             db["status"], db["detail"], "Free: 500 MB database, pauses after 7 days idle",
             "Amazon RDS for PostgreSQL / Aurora Serverless", test=True, env_vars=["DATABASE_URL"],
             dashboard="https://supabase.com/dashboard"),
        item("supabase_storage", "Supabase Storage", "Data & storage",
             "Private bucket for uploaded demand-history CSVs (forecast provenance) and the certificate fallback.",
             "live" if storage.configured() else "off",
             f"Bucket '{storage.bucket()}'" if storage.configured() else "Uploads are processed but not archived.",
             "Free: 1 GB storage", "Amazon S3", test=True,
             env_vars=["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"], dashboard="https://supabase.com/dashboard"),
        item("upstash_redis", "Upstash Redis", "Data & storage",
             "Cache for road routes and the network summary; once-a-day alert guards; delivery locks; rate limits.",
             "live" if cache.backend_name() == "upstash" else "fallback",
             "Serverless Redis over REST" if cache.backend_name() == "upstash" else "In-process cache (single instance only).",
             "Free: 500k commands/month, 256 MB", "Amazon ElastiCache (Redis OSS) / MemoryDB", test=True,
             env_vars=["UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"], dashboard="https://console.upstash.com/"),

        # ── Messaging & scheduling ─────────────────────────────────────────
        item("qstash", "Upstash QStash", "Messaging & scheduling",
             "Message queue for notification delivery (3 retries, then dead-letter queue) and the cron scheduler for jobs.",
             "live" if qstash.configured() else "fallback",
             ("Signed callbacks verified" if qstash.can_verify() else "Signing keys missing — callbacks will be refused")
             if qstash.configured() else "Delivering in a background thread; jobs need an external trigger.",
             "Free: 1,000 messages/day, 10 schedules", "Amazon SQS + Lambda (queue), EventBridge Scheduler (cron)",
             test=True, env_vars=["QSTASH_TOKEN", "QSTASH_CURRENT_SIGNING_KEY", "QSTASH_NEXT_SIGNING_KEY", "PUBLIC_API_URL"],
             dashboard="https://console.upstash.com/qstash"),
        item("pusher", "Pusher Channels", "Messaging & scheduling",
             "Pushes transfer changes and alerts to every open console of a facility, on a private signed channel.",
             "live" if realtime.configured() else "fallback",
             f"Cluster {env('PUSHER_CLUSTER')}" if realtime.configured() else "Consoles poll every 2.5 s instead.",
             "Sandbox: 200k messages/day, 100 connections", "Amazon API Gateway WebSockets / AWS AppSync",
             test=True, env_vars=["PUSHER_APP_ID", "PUSHER_KEY", "PUSHER_SECRET", "PUSHER_CLUSTER"],
             dashboard="https://dashboard.pusher.com/"),

        # ── Notifications ──────────────────────────────────────────────────
        item("email_gmail", "Gmail API (Google Cloud)", "Notifications",
             "Primary email provider: alerts and the daily digest, sent as your own Gmail address so they reach the inbox.",
             "live" if email_providers["gmail"]["configured"] else "off",
             f"Sending as {env('GMAIL_SENDER')}" if email_providers["gmail"]["configured"] else "Needs a refresh token (scripts/gmail_refresh_token.py).",
             "Free: ~500 emails/day per account", "Amazon SES", test=True,
             env_vars=["GMAIL_SENDER", "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"],
             dashboard="https://console.cloud.google.com/apis/library/gmail.googleapis.com"),
        item("email_brevo", "Brevo", "Notifications",
             "Failover email provider over the Brevo transactional API.",
             "live" if email_providers["brevo"]["configured"] else "off",
             "Configured" if email_providers["brevo"]["configured"] else "Not configured.",
             "Free: 300 emails/day", "Amazon SES", test=True,
             env_vars=["BREVO_API_KEY", "BREVO_SENDER_EMAIL"], dashboard="https://app.brevo.com/"),
        item("email_resend", "Resend", "Notifications",
             "Second failover email provider (developer-first email API).",
             "live" if email_providers["resend"]["configured"] else "off",
             "Configured" if email_providers["resend"]["configured"] else "Not configured.",
             "Free: 3,000 emails/month, 100/day", "Amazon SES", test=True,
             env_vars=["RESEND_API_KEY", "RESEND_FROM"], dashboard="https://resend.com/emails"),
        item("telegram", "Telegram Bot API", "Notifications",
             "Phone alerts for each facility's linked chats, plus /stock and /transfers commands.",
             "live" if telegram.configured() else "off",
             (f"@{telegram.bot_username()}" + (" · webhook" if env("TELEGRAM_WEBHOOK_SECRET") else " · polling mode"))
             if telegram.configured() else "Not configured.",
             "Free", "Amazon SNS (mobile push / SMS)", test=True,
             env_vars=["TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET"], dashboard="https://t.me/BotFather"),
        item("discord", "Discord webhook", "Notifications",
             "Network operations channel: every transfer across all facilities, failures and daily network summary.",
             "live" if discord.configured() else "off",
             "Configured" if discord.configured() else "Not configured.",
             "Free", "Amazon SNS → AWS Chatbot", test=True, env_vars=["DISCORD_WEBHOOK_URL"]),

        # ── Observability ──────────────────────────────────────────────────
        item("sentry", "Sentry", "Observability",
             "Error tracking with stack traces and release tagging for the API and the console.",
             "live" if sentry_status()["active"] else "off",
             f"Environment '{sentry_status()['environment']}'" if sentry_status()["active"] else "Not configured.",
             "Developer: 5k errors/month", "Amazon CloudWatch + AWS X-Ray", test=True,
             env_vars=["SENTRY_DSN"], dashboard="https://sentry.io/"),
        item("betterstack_logs", "Better Stack Logs", "Observability",
             "Centralised, searchable JSON logs from the API that survive restarts and redeploys.",
             "live" if betterstack_status()["active"] else "off",
             "Shipping logs" if betterstack_status()["active"] else "Logs go to stdout (Render dashboard) only.",
             "Free: 3 GB/month, 3-day retention", "Amazon CloudWatch Logs", test=True,
             env_vars=["BETTERSTACK_SOURCE_TOKEN", "BETTERSTACK_INGESTING_HOST"], dashboard="https://telemetry.betterstack.com/"),
        item("heartbeats", "Job heartbeats (Better Stack / Healthchecks.io)", "Observability",
             "Dead man's switch: alerts if a scheduled job stops running.",
             "live" if any(beats.values()) else "off",
             ", ".join(f"{job}: {'on' if on else 'off'}" for job, on in beats.items()),
             "Free: Better Stack 10 monitors · Healthchecks.io 20 checks",
             "CloudWatch alarm on EventBridge invocation metrics",
             env_vars=[f"HEARTBEAT_URL_{j.upper().replace('-', '_')}" for j in JOB_NAMES]),
        item("uptimerobot", "UptimeRobot", "Observability",
             "Pings /api/v1/health every 5 minutes, emails on outage, hosts a public status page.",
             "live" if cache.get_json("monitor:uptimerobot:last") else "off",
             _monitor_seen("uptimerobot"), "Free: 50 monitors, 5-minute interval",
             "Amazon CloudWatch Synthetics / Route 53 health checks",
             env_vars=["STATUS_PAGE_URL"], dashboard=env("STATUS_PAGE_URL") or "https://dashboard.uptimerobot.com/"),

        # ── Security & identity ────────────────────────────────────────────
        item("google_oauth", "Google Identity (OAuth 2.0)", "Security & identity",
             "Who you are: Google sign-in, verified server side against the token's audience.",
             "live" if env("GOOGLE_CLIENT_ID") else "off",
             "Configured" if env("GOOGLE_CLIENT_ID") else ("Dev sign-in only" if env("ALLOW_DEV_SIGNIN") == "1" else "Not configured."),
             "Free", "Amazon Cognito (Google federation)", env_vars=["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
             dashboard="https://console.cloud.google.com/apis/credentials"),
        item("doppler", "Doppler", "Security & identity",
             "One place for every secret above, per environment, synced into Render and Vercel.",
             "live" if doppler else "build",
             f"Config '{env('DOPPLER_CONFIG')}'" if doppler else "Used from the CLI (doppler run) and dashboard sync.",
             "Developer: free, up to 3 users", "AWS Secrets Manager / SSM Parameter Store",
             dashboard="https://dashboard.doppler.com/"),

        # ── AWS compliance stack ───────────────────────────────────────────
        stack("aws_sns", "Amazon SNS", "Fans out every custody event to the ledger queue; e-mails alerts to the compliance officer.",
              "Publishing custody events", "Always free: 1M publishes, 1,000 email deliveries/month", True,
              ["AWS_SNS_CUSTODY_TOPIC_ARN", "AWS_SNS_ALERTS_TOPIC_ARN"]),
        stack("aws_sqs", "Amazon SQS (+ DLQ)", "Buffers custody events for the Lambda; poison messages move to a dead-letter queue.",
              "Deployed by the stack", "Always free: 1M requests/month"),
        stack("aws_lambda", "AWS Lambda", "Writes the hash-chained custody ledger; builds the daily compliance report.",
              "Deployed by the stack", "Always free: 1M requests, 400k GB-s/month"),
        stack("aws_dynamodb", "Amazon DynamoDB", "Append-only, hash-chained custody ledger, independent of the app database.",
              "Deployed by the stack", "Always free: 25 GB, 25 RCU/WCU"),
        stack("aws_eventbridge", "Amazon EventBridge Scheduler", "Invokes the compliance-report Lambda every morning.",
              "Deployed by the stack", "Free: 14M invocations/month"),
        stack("aws_s3", "Amazon S3", "Private, encrypted, versioned store for chain-of-custody PDF certificates.",
              f"Bucket {env('AWS_S3_BUCKET') or '—'}", "Free plan credits (5 GB standard)", True, ["AWS_S3_BUCKET"]),
        item("aws_ses", "Amazon SES", "AWS compliance stack", "Fourth email provider in the failover chain.",
             "live" if aws.ses_enabled() else "off",
             f"Sender {env('AWS_SES_SENDER')} (sandbox: verified recipients only)" if aws.ses_enabled() else "Not configured.",
             "Free plan credits; sandbox 200 emails/day", "— (this is the AWS service)", True, ["AWS_SES_SENDER"]),
        stack("aws_iam", "AWS IAM", "Least privilege: the API's user may only publish to two topics, write certificates and send mail.",
              "Credentials valid (use Test to see the identity)", "Free", True, ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]),
        stack("aws_cloudwatch", "Amazon CloudWatch", "Lambda logs; alarms on DLQ depth and Lambda errors notify the alerts topic.",
              "Alarms deployed by the stack", "Always free: 10 alarms, 5 GB logs"),
        stack("aws_cloudformation", "AWS CloudFormation", "The whole AWS side as one template: aws/plateletiq-aws.yaml.",
              "Stack deployed", "Free"),
        stack("aws_budgets", "AWS Budgets", "Cost guardrail: emails at 80% of a US$1 monthly budget, so the free tier stays free.",
              "Budget deployed by the stack", "Free (first two budgets)"),

        # ── DevOps ─────────────────────────────────────────────────────────
        item("github", "GitHub + GitHub Actions", "DevOps",
             "Source control; CI builds the console, tests the API, lints the AWS template and builds the Docker image on every push.",
             "build", ".github/workflows/ci.yml and run-job.yml", "Free for public repos; 2,000 min/month private",
             "AWS CodeCommit + CodeBuild + CodePipeline", dashboard="https://github.com/"),
        item("docker", "Docker + Compose", "DevOps",
             "One command brings up Postgres, the API and the console locally, matching production.",
             "live" if os.path.exists("/.dockerenv") else "build",
             "Running inside a container" if os.path.exists("/.dockerenv") else "backend/Dockerfile, docker-compose.yml",
             "Free (Docker Desktop personal)", "Amazon ECR + ECS / Fargate"),

        # ── Maps & logistics (pre-existing) ─────────────────────────────────
        item("routing", "Mapbox / OSRM + OpenStreetMap", "Maps & logistics",
             "Road routes and ETAs for each transfer; map tiles for live tracking.",
             "live", "Mapbox Directions (traffic-aware)" if routing_mapbox else "Public OSRM server (no key needed)",
             "OSRM/OSM free; Mapbox 100k requests/month", "Amazon Location Service", test=True,
             env_vars=["MAPBOX_ACCESS_TOKEN"]),
        item("shiprocket", "Shiprocket", "Maps & logistics",
             "Courier booking, airway bills, pickup and tracking webhooks.",
             "live" if courier_live else "fallback",
             "Live bookings" if courier_live else "Simulated courier (says so in the UI).",
             "Pay per shipment (simulated here)", "— (third-party logistics)",
             env_vars=["TRANSPORT_MODE", "SHIPROCKET_EMAIL", "SHIPROCKET_PASSWORD"]),
    ]


def summary(items: List[dict]) -> dict:
    counts: dict = {}
    for entry in items:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return {"total": len(items), **counts}
