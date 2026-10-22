"""
Job heartbeats — "dead man's switch" monitoring for scheduled work.

An uptime monitor tells you the API answers. It cannot tell you that the 8 AM
digest silently stopped running. Each scheduled job pings its own heartbeat
URL when it finishes; the monitoring service (Better Stack heartbeats or
Healthchecks.io, both free) raises an alert if a ping does not arrive on time.

    HEARTBEAT_URL_EXPIRY_SWEEP=https://uptime.betterstack.com/api/v1/heartbeat/...
    HEARTBEAT_URL_DAILY_DIGEST=https://hc-ping.com/<uuid>
    HEARTBEAT_URL_FORECAST_REFRESH=...

A failed job pings ``<url>/fail`` so the alert fires immediately rather than
waiting for the grace period.
"""

import httpx

from cloud.config import env


def url_for(job: str) -> str:
    return env(f"HEARTBEAT_URL_{job.upper().replace('-', '_')}")


def ping(job: str, ok: bool = True) -> bool:
    url = url_for(job)
    if not url:
        return False
    try:
        target = url if ok else url.rstrip("/") + "/fail"
        httpx.get(target, timeout=5)
        return True
    except Exception:
        return False


def status(jobs: list) -> dict:
    return {job: bool(url_for(job)) for job in jobs}
