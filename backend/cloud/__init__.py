"""
Adapters for the managed cloud services PlateletIQ runs on.

Every module here follows the same rule as the courier and routing adapters:
an integration switches on from its environment variables and, when those are
absent, the app keeps working and says so (``status()`` on each module, and
``GET /api/v1/system/integrations``). Nothing claims to be live unless it is.

    config.py         environment helpers, public URLs
    observability.py  Sentry error tracking, Better Stack log shipping
    cache.py          Upstash Redis (REST) with an in-process fallback
    email.py          Gmail API, Brevo, Resend, Amazon SES — with failover
    telegram.py       Telegram Bot API (per-facility phone alerts)
    discord.py        Discord webhook (network operations channel)
    realtime.py       Pusher Channels (live in-app events)
    qstash.py         Upstash QStash (queued delivery, retries, DLQ, cron)
    aws.py            Amazon SNS, S3, SES (custody ledger, certificates)
    storage.py        Supabase Storage (uploaded demand history)
    heartbeat.py      job heartbeats (Better Stack / Healthchecks.io)
    certificates.py   chain-of-custody PDF certificate
    registry.py       the catalogue the Cloud services screen renders
"""
