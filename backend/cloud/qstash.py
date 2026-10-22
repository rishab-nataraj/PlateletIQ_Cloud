"""
Upstash QStash — the message queue and scheduler.

Two jobs, one service:

**Queued delivery.** When a transfer changes state, the API writes the
notification rows and returns immediately. It then publishes a small message
to QStash naming the notification; QStash calls back
``POST /api/v1/notifications/deliver``, which performs the slow, failure-prone
work (email, Telegram, Discord). A non-2xx answer makes QStash retry with
backoff; once retries are exhausted the message lands in QStash's
dead-letter queue and our failure callback marks the notification
``DEAD_LETTER`` so it is visible rather than silently lost. This is the
SQS + Lambda pattern, delivered over plain HTTPS to a free web service.

**Cron.** QStash schedules call ``/api/v1/jobs/*`` — the hourly expiry sweep,
the 8 AM daily digest and the nightly forecast refresh
(``scripts/setup_qstash_schedules.py`` creates them). A side effect worth
having on Render's free tier: the call wakes a spun-down instance.

Every inbound call carries an ``Upstash-Signature`` JWT signed with our
signing keys; ``verify`` checks it, including a hash of the exact body, so a
forged or replayed request to the job endpoints is refused.
"""

import base64
import hashlib
from typing import Optional

from cloud.config import env, public_api_url

_client = None


class QStashError(Exception):
    pass


def configured() -> bool:
    """Publishing needs a token *and* a public URL QStash can call back."""
    return bool(env("QSTASH_TOKEN") and public_api_url())


def can_verify() -> bool:
    return bool(env("QSTASH_CURRENT_SIGNING_KEY") and env("QSTASH_NEXT_SIGNING_KEY"))


def _qstash():
    global _client
    if _client is None and env("QSTASH_TOKEN"):
        from qstash import QStash

        _client = QStash(env("QSTASH_TOKEN"), base_url=env("QSTASH_URL") or None)
    return _client


def publish(path: str, body: dict, retries: int = 3, deduplication_id: Optional[str] = None,
            failure_path: Optional[str] = None, delay: Optional[str] = None) -> str:
    """Queue an HTTP POST of ``body`` to ``PUBLIC_API_URL + path``. Returns the message id."""
    client = _qstash()
    if not client or not public_api_url():
        raise QStashError("QStash is not configured (QSTASH_TOKEN and PUBLIC_API_URL are required).")
    base = public_api_url()
    response = client.message.publish_json(
        url=f"{base}{path}",
        body=body,
        retries=retries,
        deduplication_id=deduplication_id,
        failure_callback=f"{base}{failure_path}" if failure_path else None,
        delay=delay,
        label="plateletiq",
    )
    return getattr(response, "message_id", "") or ""


def verify(signature: Optional[str], body: bytes, request_path: str) -> dict:
    """
    Check an ``Upstash-Signature`` header. Returns the verified claims.

    Tried against the current signing key, then the next one, so rotating
    keys in the Upstash console never drops in-flight messages.
    """
    if not signature:
        raise QStashError("Missing Upstash-Signature header.")
    if not can_verify():
        raise QStashError("QStash signing keys are not configured.")

    import jwt

    expected_body_hash = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("=")
    last_error = None
    for key in (env("QSTASH_CURRENT_SIGNING_KEY"), env("QSTASH_NEXT_SIGNING_KEY")):
        try:
            claims = jwt.decode(
                signature,
                key,
                algorithms=["HS256"],
                issuer="Upstash",
                options={"require": ["iss", "sub", "exp", "nbf"]},
                leeway=30,
            )
        except Exception as exc:
            last_error = exc
            continue
        if str(claims.get("body", "")).rstrip("=") != expected_body_hash:
            raise QStashError("Signature does not match the request body.")
        # The subject is the URL QStash was told to call. Render terminates
        # TLS in front of us, so compare paths rather than full URLs.
        if not str(claims.get("sub", "")).split("?")[0].endswith(request_path):
            raise QStashError("Signature was issued for a different endpoint.")
        return claims
    raise QStashError(f"Signature did not verify: {last_error}")


def ensure_schedule(schedule_id: str, path: str, cron: str, body: Optional[dict] = None) -> str:
    """Create or update (same id) a cron schedule that POSTs to ``path``."""
    client = _qstash()
    if not client or not public_api_url():
        raise QStashError("QStash is not configured (QSTASH_TOKEN and PUBLIC_API_URL are required).")
    return client.schedule.create_json(
        destination=f"{public_api_url()}{path}",
        cron=cron,
        body=body or {"trigger": "qstash-schedule"},
        retries=2,
        schedule_id=schedule_id,
        label="plateletiq",
    )


def list_schedules() -> list:
    client = _qstash()
    if not client:
        return []
    return [
        {
            "id": s.schedule_id,
            "cron": s.cron,
            "destination": s.destination,
            "paused": getattr(s, "paused", False),
        }
        for s in client.schedule.list()
    ]


def dlq_size() -> Optional[int]:
    client = _qstash()
    if not client:
        return None
    try:
        return len(client.dlq.list().messages)
    except Exception:
        return None


def status() -> dict:
    return {
        "configured": configured(),
        "token": bool(env("QSTASH_TOKEN")),
        "public_url": public_api_url() or None,
        "signature_verification": can_verify(),
    }
