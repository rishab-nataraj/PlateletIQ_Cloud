"""
Authentication for machine-to-machine endpoints (queue callbacks, cron jobs).

Two accepted credentials, checked in this order:

  1. An ``Upstash-Signature`` JWT from QStash, verified against our signing
     keys and a hash of the exact body.
  2. ``X-Jobs-Secret: <JOBS_SECRET>`` — for the GitHub Actions workflow and
     for running a job by hand with curl.

Anything else is a 401. With neither configured these endpoints are closed.
"""

import hmac

from fastapi import HTTPException, Request

from cloud import qstash
from cloud.config import env


async def verify_internal(request: Request) -> dict:
    body = await request.body()

    signature = request.headers.get("upstash-signature")
    if signature:
        try:
            claims = qstash.verify(signature, body, request.url.path)
            return {"via": "qstash", "message_id": request.headers.get("upstash-message-id"), "claims": claims}
        except qstash.QStashError as exc:
            raise HTTPException(status_code=401, detail=f"QStash signature rejected: {exc}")

    secret = env("JOBS_SECRET")
    supplied = request.headers.get("x-jobs-secret", "")
    if secret and supplied and hmac.compare_digest(secret, supplied):
        return {"via": request.headers.get("x-trigger", "secret")}

    raise HTTPException(
        status_code=401,
        detail="This endpoint is for QStash or a holder of JOBS_SECRET.",
    )
