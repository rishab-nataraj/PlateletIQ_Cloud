"""
Notification centre: inbox, settings, Telegram linking, and the queue callbacks.

Facility-facing endpoints read the facility off the JWT like every other
stock-touching endpoint. ``/deliver`` and ``/dead-letter`` are called by
QStash and accept only a verified QStash signature (or ``JOBS_SECRET``).
"""

import base64
import datetime
import json
import re
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import require_facility
from cloud import discord, qstash, realtime, telegram
from cloud import email as email_service
from cloud.config import env, mask_email
from database import get_db
from models.notification import Notification, TelegramLink
from routers.internal_auth import verify_internal
from services import notifications, telegram_links

router = APIRouter(prefix="/notifications", tags=["notifications"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── Inbox ────────────────────────────────────────────────────────────────────

@router.get("")
def list_notifications(
    unread_only: bool = False,
    limit: int = Query(40, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_facility),
):
    query = db.query(Notification).filter(Notification.bank_id == current_user["bank_id"])
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    rows = query.order_by(Notification.created_at.desc()).limit(limit).all()
    return {"data": [notifications.serialise(n, db=db) for n in rows], "error": None}


@router.get("/unread-count")
def unread_count(db: Session = Depends(get_db), current_user: dict = Depends(require_facility)):
    count = (
        db.query(Notification)
        .filter(Notification.bank_id == current_user["bank_id"], Notification.read_at.is_(None))
        .count()
    )
    return {"data": {"unread": count}, "error": None}


@router.post("/{notification_id}/read")
def mark_read(notification_id: str, db: Session = Depends(get_db), current_user: dict = Depends(require_facility)):
    notification = db.get(Notification, notification_id)
    if not notification or notification.bank_id != current_user["bank_id"]:
        raise HTTPException(status_code=404, detail="Notification not found.")
    notification.read_at = notification.read_at or datetime.datetime.utcnow()
    db.commit()
    return {"data": {"id": notification_id, "read": True}, "error": None}


@router.post("/read-all")
def mark_all_read(db: Session = Depends(get_db), current_user: dict = Depends(require_facility)):
    now = datetime.datetime.utcnow()
    updated = (
        db.query(Notification)
        .filter(Notification.bank_id == current_user["bank_id"], Notification.read_at.is_(None))
        .update({Notification.read_at: now}, synchronize_session=False)
    )
    db.commit()
    return {"data": {"marked": updated}, "error": None}


# ── Settings ─────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    email_enabled: Optional[bool] = None
    telegram_enabled: Optional[bool] = None
    digest_enabled: Optional[bool] = None
    expiry_alerts_enabled: Optional[bool] = None
    extra_emails: Optional[List[str]] = None


def _settings_payload(db: Session, bank_id: str) -> dict:
    settings = notifications.get_settings(db, bank_id)
    recipients = notifications.facility_emails(db, bank_id, settings)
    chats = notifications.linked_chats(db, bank_id)
    return {
        **settings.as_dict(),
        "email_recipients": [mask_email(r) for r in recipients],
        "telegram_chats": [{"id": c.id, "label": c.chat_label} for c in chats],
        "channels": {
            "email": {"available": bool(email_service.provider_chain()), "providers": email_service.provider_chain()},
            "telegram": {"available": telegram.configured(), "bot": telegram.bot_username() if telegram.configured() else None},
            "realtime": {"available": realtime.configured()},
            "discord": {"available": discord.configured()},
            "queue": {"available": qstash.configured(), "mode": "qstash" if qstash.configured() else "inline"},
        },
    }


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), current_user: dict = Depends(require_facility)):
    payload = _settings_payload(db, current_user["bank_id"])
    db.commit()  # persists default settings the first time they are read
    return {"data": payload, "error": None}


@router.put("/settings")
def update_settings(
    req: SettingsUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_facility),
):
    settings = notifications.get_settings(db, current_user["bank_id"])
    for field in ("email_enabled", "telegram_enabled", "digest_enabled", "expiry_alerts_enabled"):
        value = getattr(req, field)
        if value is not None:
            setattr(settings, field, value)
    if req.extra_emails is not None:
        cleaned = []
        for address in req.extra_emails:
            address = address.strip().lower()
            if not address:
                continue
            if not EMAIL_RE.match(address):
                raise HTTPException(status_code=400, detail=f"'{address}' is not a valid email address.")
            if address not in cleaned:
                cleaned.append(address)
        if len(cleaned) > 10:
            raise HTTPException(status_code=400, detail="At most 10 extra recipients per facility.")
        settings.extra_emails = ",".join(cleaned)
    settings.updated_at = datetime.datetime.utcnow()
    db.commit()
    return {"data": _settings_payload(db, current_user["bank_id"]), "error": None}


@router.post("/test")
def send_test(db: Session = Depends(get_db), current_user: dict = Depends(require_facility)):
    """Send one notification to this facility through every configured channel."""
    from cloud import cache

    if cache.hit(f"test-notify:{current_user['bank_id']}", 60) > 3:
        raise HTTPException(status_code=429, detail="Three test notifications a minute is plenty. Try again shortly.")
    created = notifications.notify(
        db,
        bank_ids=[current_user["bank_id"]],
        event_type="TEST",
        title="Test notification from PlateletIQ",
        body=(
            f"{current_user.get('name') or current_user.get('email')} sent this from the Notifications screen "
            "to check every channel end to end."
        ),
        severity="info",
        link=notifications.console_link("notifications"),
        data={"details": [("Facility", current_user.get("bank_name")), ("Sent at (UTC)", datetime.datetime.utcnow().strftime("%H:%M:%S"))]},
        ops=True,
    )
    return {"data": [notifications.serialise(n, db=db) for n in created], "error": None}


# ── Telegram linking ─────────────────────────────────────────────────────────

@router.post("/telegram/link")
def create_telegram_link(db: Session = Depends(get_db), current_user: dict = Depends(require_facility)):
    if not telegram.configured():
        raise HTTPException(status_code=503, detail="Telegram is not configured on this server (TELEGRAM_BOT_TOKEN).")
    link = TelegramLink(
        id=f"TGL-{uuid.uuid4().hex[:10].upper()}",
        bank_id=current_user["bank_id"],
        link_code=telegram_links.new_code(),
        status="PENDING",
        created_by=current_user.get("sub"),
    )
    db.add(link)
    db.commit()
    deep_link = telegram.deep_link(link.link_code)
    if not deep_link:
        raise HTTPException(status_code=502, detail="Could not read the bot's username from Telegram.")
    return {
        "data": {
            "id": link.id,
            "code": link.link_code,
            "deep_link": deep_link,
            "bot": telegram.bot_username(),
            "expires_in_minutes": int(telegram_links.CODE_TTL.total_seconds() // 60),
            "webhook": bool(env("TELEGRAM_WEBHOOK_SECRET")),
        },
        "error": None,
    }


@router.get("/telegram/links/{link_id}")
def telegram_link_status(link_id: str, db: Session = Depends(get_db), current_user: dict = Depends(require_facility)):
    link = db.get(TelegramLink, link_id)
    if not link or link.bank_id != current_user["bank_id"]:
        raise HTTPException(status_code=404, detail="Link not found.")
    if link.status == "PENDING" and telegram.configured() and not env("TELEGRAM_WEBHOOK_SECRET"):
        # No webhook (local development): look for the /start message ourselves.
        try:
            telegram_links.poll_updates(db)
            db.refresh(link)
        except Exception as exc:
            return {"data": {"id": link.id, "status": link.status, "warning": str(exc)}, "error": None}
    return {"data": {"id": link.id, "status": link.status, "chat": link.chat_label}, "error": None}


@router.delete("/telegram/links/{link_id}")
def revoke_telegram_link(link_id: str, db: Session = Depends(get_db), current_user: dict = Depends(require_facility)):
    link = db.get(TelegramLink, link_id)
    if not link or link.bank_id != current_user["bank_id"]:
        raise HTTPException(status_code=404, detail="Link not found.")
    link.status = "REVOKED"
    db.commit()
    if link.chat_id and telegram.configured():
        try:
            telegram.send_message(link.chat_id, "This chat was unlinked from PlateletIQ alerts in the console.")
        except Exception:
            pass
    return {"data": {"id": link.id, "status": link.status}, "error": None}


# ── Queue callbacks (QStash) ─────────────────────────────────────────────────

@router.post("/deliver")
async def deliver_from_queue(request: Request, response: Response):
    """QStash calls this with ``{"notification_id"}``. 503 asks QStash to retry."""
    auth = await verify_internal(request)
    payload = json.loads((await request.body()) or b"{}")
    notification_id = payload.get("notification_id")
    if not notification_id:
        raise HTTPException(status_code=400, detail="notification_id is required.")

    result = notifications.deliver(notification_id)
    result["queue"] = auth.get("via")
    if result.get("retry"):
        # Some channel failed transiently; QStash will call again with backoff
        # and, after its retries, move the message to the dead-letter queue.
        response.status_code = 503
    return result


@router.post("/dead-letter")
async def dead_letter(request: Request, db: Session = Depends(get_db)):
    """QStash's failure callback, sent once retries are exhausted."""
    await verify_internal(request)
    payload = json.loads((await request.body()) or b"{}")
    notification_id = None
    try:
        source = json.loads(base64.b64decode(payload.get("sourceBody") or "") or b"{}")
        notification_id = source.get("notification_id")
    except Exception:
        notification_id = payload.get("notification_id")
    if not notification_id:
        return {"status": "ignored"}
    reason = f"QStash gave up (last status {payload.get('status')})"
    notifications.mark_dead_letter(db, notification_id, reason)
    return {"status": "recorded", "notification_id": notification_id}
