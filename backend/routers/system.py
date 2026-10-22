"""
System status for the Cloud services screen.

``GET  /system/integrations``     every cloud service, live status computed now
``POST /system/test/{key}``       exercise one service end to end, timed
``POST /system/qstash-echo``      target of the QStash round-trip test
``GET  /system/client-config``    public values the browser needs (Pusher key)

Test calls are rate-limited per facility (Upstash Redis) so a demo screen
cannot exhaust a free quota.
"""

import datetime
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from auth import require_facility
from cloud import aws, cache, discord, qstash, realtime, registry, storage, telegram
from cloud import email as email_service
from cloud.config import app_env, env, release
from cloud.observability import log_event, sentry_status
from database import get_db
from models.bank import Bank
from models.notification import Notification, NotificationDelivery
from routers.internal_auth import verify_internal
from services import custody_stream, notifications

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/client-config")
def client_config():
    return {
        "data": {
            "realtime": realtime.client_config(),
            "telegram_bot": telegram.bot_username() if telegram.configured() else None,
            "environment": app_env(),
            "release": release(),
            "status_page_url": env("STATUS_PAGE_URL") or None,
        },
        "error": None,
    }


@router.get("/integrations")
def integrations(db: Session = Depends(get_db), current_user: dict = Depends(require_facility)):
    items = registry.catalogue()
    since = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
    rows = (
        db.query(NotificationDelivery.channel, NotificationDelivery.status, func.count(NotificationDelivery.id))
        .join(Notification, Notification.id == NotificationDelivery.notification_id)
        .filter(Notification.created_at >= since)
        .group_by(NotificationDelivery.channel, NotificationDelivery.status)
        .all()
    )
    deliveries: dict = {}
    for channel, status, count in rows:
        deliveries.setdefault(channel, {})[status] = count

    return {
        "data": {
            "services": items,
            "summary": registry.summary(items),
            "deliveries_24h": deliveries,
            "queue": {
                "mode": "qstash" if qstash.configured() else "inline",
                "dead_letter_queue": qstash.dlq_size() if qstash.configured() else None,
            },
            "custody_stream": custody_stream.status(),
            "email_chain": email_service.provider_chain(),
            "environment": app_env(),
            "release": release(),
        },
        "error": None,
    }


# ── Tests ────────────────────────────────────────────────────────────────────

def _caller_email(current_user: dict) -> str:
    address = (current_user.get("email") or "").lower()
    if not email_service.is_deliverable(address):
        raise HTTPException(
            status_code=400,
            detail=f"Your sign-in address ({address or 'none'}) cannot receive mail. Sign in with a real address to test email.",
        )
    return address


def _test_email(provider: str, current_user: dict) -> dict:
    check, send, label = email_service.PROVIDERS[provider]
    if not check():
        raise HTTPException(status_code=400, detail=f"{label} is not configured.")
    to = _caller_email(current_user)
    from services import email_templates

    html, text_body = email_templates.render(
        f"{label} test",
        [f"This message was sent through {label} from the Cloud services screen to confirm the integration works."],
        details=[("Provider", label), ("Environment", app_env()), ("Sent (UTC)", datetime.datetime.utcnow().strftime("%H:%M:%S"))],
        severity="success",
        facility_name=current_user.get("bank_name"),
    )
    message_id = send([to], f"[PlateletIQ] {label} test", html, text_body)
    return {"message": f"Sent to {to}", "id": message_id}


def _run_test(key: str, db: Session, current_user: dict) -> dict:
    bank_id = current_user["bank_id"]

    if key == "supabase_postgres":
        started = time.perf_counter()
        db.execute(text("SELECT 1"))
        return {
            "message": f"{db.bind.dialect.name} answered SELECT 1",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    if key == "upstash_redis":
        result = cache.ping()
        if not result.get("ok"):
            raise RuntimeError(result.get("error"))
        return {"message": f"{result['backend']} round trip", "latency_ms": result.get("latency_ms")}

    if key == "qstash":
        if not qstash.configured():
            raise HTTPException(status_code=400, detail="QStash is not configured.")
        message_id = qstash.publish(
            "/api/v1/system/qstash-echo",
            {"bank_id": bank_id, "requested_by": current_user.get("name"), "sent_at": time.time()},
            retries=1,
        )
        return {"message": "Queued. A 'QStash round trip' notification arrives when QStash calls back.", "id": message_id}

    if key == "pusher":
        if not realtime.configured():
            raise HTTPException(status_code=400, detail="Pusher is not configured.")
        realtime.publish(bank_id, "notification", {
            "id": f"TEST-{uuid.uuid4().hex[:6]}",
            "event_type": "REALTIME_TEST",
            "severity": "info",
            "title": "Pusher realtime test",
            "body": "Delivered over your facility's private channel.",
            "created_at": datetime.datetime.utcnow().isoformat(),
            "ephemeral": True,
        })
        return {"message": f"Event pushed to {realtime.facility_channel(bank_id)} — watch for the toast."}

    if key.startswith("email_"):
        return _test_email(key.split("_", 1)[1], current_user)

    if key == "aws_ses":
        return _test_email("ses", current_user)

    if key == "telegram":
        chats = notifications.linked_chats(db, bank_id)
        if not telegram.configured():
            raise HTTPException(status_code=400, detail="Telegram is not configured.")
        if not chats:
            raise HTTPException(status_code=400, detail="No Telegram chat is linked to this facility yet (Notifications → Telegram).")
        for chat in chats:
            telegram.send_message(chat.chat_id, f"🔵 <b>Telegram test</b>\nSent from the Cloud services screen for {telegram.escape(current_user.get('bank_name') or bank_id)}.")
        return {"message": f"Sent to {len(chats)} linked chat(s)"}

    if key == "discord":
        if not discord.configured():
            raise HTTPException(status_code=400, detail="Discord is not configured.")
        message_id = discord.send(
            "Discord ops channel test",
            f"Sent by {current_user.get('name')} at {current_user.get('bank_name')}.",
            "info",
        )
        return {"message": "Posted to the ops channel", "id": message_id}

    if key == "sentry":
        if not sentry_status()["active"]:
            raise HTTPException(status_code=400, detail="Sentry is not configured.")
        import sentry_sdk

        event_id = sentry_sdk.capture_exception(
            RuntimeError(f"PlateletIQ Sentry test from {bank_id} (intentional)")
        )
        return {"message": "Test exception captured — open Sentry → Issues", "id": event_id}

    if key == "betterstack_logs":
        log_event("Better Stack test log line", bank_id=bank_id, requested_by=current_user.get("email"), test=True)
        return {"message": "Logged — search Better Stack for 'Better Stack test log line'"}

    if key == "supabase_storage":
        if not storage.configured():
            raise HTTPException(status_code=400, detail="Supabase Storage is not configured.")
        stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        stored = storage.upload(f"diagnostics/{bank_id}-{stamp}.txt", f"PlateletIQ storage test {stamp}\n".encode(), "text/plain")
        return {"message": f"Uploaded {stored['path']}", "url": stored.get("url")}

    if key == "aws_iam":
        if not aws.has_credentials():
            raise HTTPException(status_code=400, detail="AWS credentials are not configured.")
        identity = aws.whoami()
        return {"message": f"Authenticated as {identity['arn']}"}

    if key == "aws_sns":
        if not aws.sns_enabled():
            raise HTTPException(status_code=400, detail="The SNS custody topic is not configured.")
        message_id = aws.publish_custody_event({
            "event_id": f"TEST-{uuid.uuid4().hex[:8]}",
            "transfer_id": f"TEST-{bank_id}",
            "status": "TEST",
            "title": "Connectivity test from the Cloud services screen",
            "actor_bank_id": bank_id,
            "occurred_at": datetime.datetime.utcnow().isoformat() + "Z",
            "environment": app_env(),
        })
        return {"message": "Published — the Lambda writes it to DynamoDB within seconds", "id": message_id}

    if key == "aws_s3":
        if not aws.s3_enabled():
            raise HTTPException(status_code=400, detail="AWS_S3_BUCKET is not configured.")
        stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        stored = aws.put_object(f"diagnostics/{bank_id}-{stamp}.txt", f"PlateletIQ S3 test {stamp}\n".encode(), "text/plain")
        return {"message": f"Wrote s3://{stored['bucket']}/{stored['key']}", "url": stored["url"]}

    if key == "routing":
        from services.routing import get_route

        banks = db.query(Bank).filter(Bank.latitude.isnot(None)).limit(2).all()
        if len(banks) < 2:
            raise HTTPException(status_code=400, detail="Need two facilities with coordinates.")
        route = get_route(banks[0].latitude, banks[0].longitude, banks[1].latitude, banks[1].longitude, use_cache=False)
        return {"message": f"{route['provider']}: {route['distance_km']} km, {route['duration_min']} min"}

    raise HTTPException(status_code=404, detail=f"No test for '{key}'.")


@router.post("/test/{key}")
def test_service(key: str, db: Session = Depends(get_db), current_user: dict = Depends(require_facility)):
    if cache.hit(f"svc-test:{current_user['bank_id']}:{key}", 60) > 5:
        raise HTTPException(status_code=429, detail="Five tests a minute per service. Try again shortly.")
    started = time.perf_counter()
    try:
        result = _run_test(key, db, current_user)
        ok = True
    except HTTPException:
        raise
    except Exception as exc:
        result, ok = {"message": f"{type(exc).__name__}: {str(exc)[:300]}"}, False
    elapsed = round((time.perf_counter() - started) * 1000)
    log_event("integration test", service=key, ok=ok, ms=elapsed, bank_id=current_user["bank_id"])
    return {"data": {"key": key, "ok": ok, "elapsed_ms": elapsed, **result}, "error": None}


@router.post("/qstash-echo")
async def qstash_echo(request: Request, db: Session = Depends(get_db)):
    """Called back by QStash during the round-trip test."""
    auth = await verify_internal(request)
    payload = json.loads((await request.body()) or b"{}")
    bank_id = payload.get("bank_id")
    if not bank_id or not db.get(Bank, bank_id):
        return {"status": "ignored"}
    sent_at = float(payload.get("sent_at") or time.time())
    notifications.notify(
        db,
        bank_ids=[bank_id],
        event_type="QSTASH_TEST",
        title="QStash round trip OK",
        body=(
            f"QStash delivered the queued message back to the API in {round(time.time() - sent_at, 1)} s "
            f"(signature verified via {auth['via']})."
        ),
        severity="success",
        channels=("realtime",),
        link=notifications.console_link("cloud"),
    )
    return {"status": "ok"}
