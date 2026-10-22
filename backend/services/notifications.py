"""
The notification pipeline.

    event in a request ──► create()  rows: Notification + one Delivery per
                          │          recipient per channel. Commit. Return.
                          ▼
                       dispatch()   realtime push now (Pusher, ~100 ms);
                          │          everything slow is handed off:
                          ├── QStash configured ──► queued message ──► POST /notifications/deliver
                          │                                         (retries, then dead-letter queue)
                          └── otherwise ──────────► background thread ──► deliver()
                                                    (the hourly sweep redrives anything stuck)

``deliver()`` is idempotent: it only touches deliveries still PENDING or
FAILED, holds a short Redis lock so two workers never send the same message
twice, and commits after each channel so a crash mid-way loses nothing.

What a notification never contains: a pickup or receipt code. Custody codes
stay on the issuing facility's screen, exactly as before.
"""

import datetime
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session

from cloud import cache, discord, qstash, realtime, telegram
from cloud import email as email_service
from cloud.config import frontend_url, mask_email
from cloud.observability import capture_exception, log_event, logger
from database import SessionLocal
from models.bank import Bank, BankMembership
from models.notification import (
    FacilityNotificationSettings,
    Notification,
    NotificationDelivery,
    TelegramLink,
)
from services import email_templates

DELIVER_PATH = "/api/v1/notifications/deliver"
DEAD_LETTER_PATH = "/api/v1/notifications/dead-letter"
MAX_ATTEMPTS = 5
EXTERNAL_CHANNELS = ("email", "telegram", "discord")
SEVERITY_ICONS = {"info": "🔵", "success": "✅", "warning": "🟠", "critical": "🔴"}

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")
_pending: set = set()


def background(fn, *args, **kwargs) -> None:
    """Run ``fn`` off the request thread; errors are logged, never raised."""

    def runner():
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            logger.exception(f"Background task {getattr(fn, '__name__', fn)} failed: {exc!r}")
            capture_exception(exc)

    future = _executor.submit(runner)
    _pending.add(future)
    future.add_done_callback(_pending.discard)


def drain(timeout: float = 15) -> None:
    """Wait for in-flight background work (graceful shutdown, tests)."""
    deadline = time.time() + timeout
    while _pending and time.time() < deadline:
        wait(list(_pending), timeout=max(0.1, deadline - time.time()))


def console_link(screen: str = "notifications", transfer_id: Optional[str] = None) -> str:
    link = f"{frontend_url()}/?screen={screen}"
    return link + (f"&transfer={transfer_id}" if transfer_id else "")


# ── Recipients ───────────────────────────────────────────────────────────────

def get_settings(db: Session, bank_id: str) -> FacilityNotificationSettings:
    settings = db.get(FacilityNotificationSettings, bank_id)
    if settings is None:
        settings = FacilityNotificationSettings(bank_id=bank_id)
        db.add(settings)
        db.flush()
    return settings


def facility_emails(db: Session, bank_id: str, settings: Optional[FacilityNotificationSettings] = None) -> List[str]:
    """
    Who gets email for a facility: its signed-in members plus any extra
    addresses added on the Notifications screen. Never the registry's public
    hospital address — that belongs to a real blood bank.
    """
    settings = settings or get_settings(db, bank_id)
    members = (
        db.query(BankMembership.email)
        .filter(BankMembership.bank_id == bank_id, BankMembership.active.is_(True))
        .all()
    )
    seen, result = set(), []
    for address in [m.email for m in members if m.email] + settings.extra_email_list:
        address = address.strip().lower()
        if address not in seen and email_service.is_deliverable(address):
            seen.add(address)
            result.append(address)
    return result


def linked_chats(db: Session, bank_id: str) -> List[TelegramLink]:
    return (
        db.query(TelegramLink)
        .filter(TelegramLink.bank_id == bank_id, TelegramLink.status == "LINKED")
        .all()
    )


def facilities_with_members(db: Session) -> List[str]:
    """Facilities somebody has actually signed in at — the ones worth alerting."""
    rows = (
        db.query(BankMembership.bank_id)
        .filter(BankMembership.active.is_(True))
        .distinct()
        .all()
    )
    linked = db.query(TelegramLink.bank_id).filter(TelegramLink.status == "LINKED").distinct().all()
    return sorted({r[0] for r in rows} | {r[0] for r in linked})


# ── Create ───────────────────────────────────────────────────────────────────

def _delivery(notification: Notification, channel: str, target: str, label: str) -> NotificationDelivery:
    return NotificationDelivery(
        id=f"DLV-{uuid.uuid4().hex[:12].upper()}",
        notification_id=notification.id,
        channel=channel,
        target=target,
        target_label=label,
        status="PENDING",
        attempts=0,
    )


def create(
    db: Session,
    *,
    bank_ids: Iterable[Optional[str]],
    event_type: str,
    title: str,
    body: str = "",
    severity: str = "info",
    transfer_id: Optional[str] = None,
    link: Optional[str] = None,
    data: Optional[dict] = None,
    email_subject: Optional[str] = None,
    email_html: Optional[str] = None,
    channels: Sequence[str] = ("realtime", "email", "telegram"),
    ops: bool = False,
    dedupe_key: Optional[str] = None,
) -> List[Notification]:
    """Write one notification per facility with its deliveries. Commits."""
    created: List[Notification] = []
    ops_attached = False

    for bank_id in dict.fromkeys(bank_ids):  # de-duplicate, keep order
        key = f"{dedupe_key}:{bank_id or 'network'}" if dedupe_key else None
        if key and db.query(Notification.id).filter(Notification.dedupe_key == key).first():
            continue

        notification = Notification(
            id=f"NTF-{uuid.uuid4().hex[:10].upper()}",
            bank_id=bank_id,
            event_type=event_type,
            severity=severity,
            title=title,
            body=body,
            transfer_id=transfer_id,
            link=link or console_link(transfer_id=transfer_id),
            data_json=json.dumps(data or {}, default=str),
            email_subject=email_subject,
            email_html=email_html,
            dispatch_status="PENDING",
            dedupe_key=key,
        )
        db.add(notification)

        deliveries = []
        if bank_id:
            settings = get_settings(db, bank_id)
            if "realtime" in channels and realtime.configured():
                deliveries.append(_delivery(notification, "realtime", realtime.facility_channel(bank_id), "Live console"))
            if "email" in channels and settings.email_enabled and email_service.provider_chain():
                for address in facility_emails(db, bank_id, settings):
                    deliveries.append(_delivery(notification, "email", address, mask_email(address)))
            if "telegram" in channels and settings.telegram_enabled and telegram.configured():
                for chat in linked_chats(db, bank_id):
                    deliveries.append(_delivery(notification, "telegram", chat.chat_id, chat.chat_label or "Telegram chat"))
        if ops and not ops_attached and discord.configured():
            deliveries.append(_delivery(notification, "discord", "ops", "#network-ops"))
            ops_attached = True

        for delivery in deliveries:
            db.add(delivery)
        if not any(d.channel in EXTERNAL_CHANNELS for d in deliveries):
            # In-app (and possibly realtime) only: nothing left to queue.
            notification.dispatch_status = "DELIVERED"
            notification.delivered_at = datetime.datetime.utcnow()
        created.append(notification)

    db.commit()
    return created


# ── Dispatch ─────────────────────────────────────────────────────────────────

def _push_realtime(db: Session, notification: Notification) -> None:
    delivery = (
        db.query(NotificationDelivery)
        .filter(NotificationDelivery.notification_id == notification.id, NotificationDelivery.channel == "realtime")
        .first()
    )
    if not delivery or not notification.bank_id:
        return
    delivery.attempts = (delivery.attempts or 0) + 1
    try:
        realtime.publish(notification.bank_id, "notification", serialise(notification, include_deliveries=False))
        delivery.status, delivery.provider = "SENT", "pusher"
        delivery.sent_at = datetime.datetime.utcnow()
    except Exception as exc:
        delivery.status, delivery.last_error = "FAILED", str(exc)[:300]
        logger.warning(f"Realtime push failed for {notification.id}: {exc!r}")


def dispatch(db: Session, notifications: List[Notification], attempt_suffix: str = "") -> None:
    inline: List[str] = []
    for notification in notifications:
        _push_realtime(db, notification)

        pending = (
            db.query(NotificationDelivery.id)
            .filter(
                NotificationDelivery.notification_id == notification.id,
                NotificationDelivery.channel.in_(EXTERNAL_CHANNELS),
                NotificationDelivery.status.in_(["PENDING", "FAILED"]),
            )
            .count()
        )
        if not pending:
            continue

        if qstash.configured():
            # Mark it queued *before* publishing: QStash can call /deliver back
            # within milliseconds, and a status written after the publish would
            # overwrite the DELIVERED that callback just recorded.
            notification.dispatch_via, notification.dispatch_status = "qstash", "QUEUED"
            db.commit()
            try:
                message_id = qstash.publish(
                    DELIVER_PATH,
                    {"notification_id": notification.id},
                    retries=3,
                    deduplication_id=f"{notification.id}{attempt_suffix}",
                    failure_path=DEAD_LETTER_PATH,
                )
                # Touch only the message id, never the status the callback owns.
                db.query(Notification).filter(Notification.id == notification.id).update(
                    {Notification.queue_message_id: message_id}, synchronize_session=False
                )
                db.commit()
                continue
            except Exception as exc:
                # The queue being down must not cost us the alert.
                logger.warning(f"QStash publish failed for {notification.id}, delivering inline: {exc!r}")
                capture_exception(exc)

        notification.dispatch_via, notification.dispatch_status = "inline", "QUEUED"
        inline.append(notification.id)

    db.commit()
    for notification_id in inline:
        background(deliver, notification_id)


def notify(db: Session, **kwargs) -> List[Notification]:
    """Create and dispatch. Call after the endpoint has committed its own work."""
    notifications = create(db, **kwargs)
    dispatch(db, notifications)
    return notifications


# ── Deliver ──────────────────────────────────────────────────────────────────

def _facility_name(db: Session, bank_id: Optional[str]) -> Optional[str]:
    if not bank_id:
        return None
    bank = db.get(Bank, bank_id)
    return bank.name if bank else bank_id


def _send(db: Session, notification: Notification, delivery: NotificationDelivery) -> tuple:
    """Send one delivery. Returns ``(provider, message_id, note)``."""
    data = notification.data

    if delivery.channel == "email":
        html = notification.email_html
        text = notification.body or notification.title
        if not html:
            html, text = email_templates.render(
                notification.title,
                [notification.body or ""],
                details=data.get("details"),
                cta=("Open PlateletIQ", notification.link),
                severity=notification.severity,
                facility_name=_facility_name(db, notification.bank_id),
            )
        result = email_service.send_email(
            [delivery.target],
            notification.email_subject or f"[PlateletIQ] {notification.title}",
            html,
            text,
        )
        failed_first = [a["provider"] for a in result["attempts"] if not a.get("ok")]
        note = f"Failed over from {', '.join(failed_first)}" if failed_first else None
        return result["provider"], result["message_id"], note

    if delivery.channel == "telegram":
        icon = SEVERITY_ICONS.get(notification.severity, "")
        lines = [f"<b>{icon} {telegram.escape(notification.title)}</b>"]
        if notification.body:
            lines.append(telegram.escape(notification.body))
        for label, value in data.get("details") or []:
            lines.append(f"<i>{telegram.escape(str(label))}:</i> {telegram.escape(str(value))}")
        message_id = telegram.send_message(delivery.target, "\n".join(lines), ("Open in PlateletIQ", notification.link))
        return "telegram", message_id, None

    if delivery.channel == "discord":
        fields = data.get("fields") or [
            {"name": str(label), "value": str(value)} for label, value in (data.get("details") or [])
        ]
        message_id = discord.send(notification.title, notification.body or "", notification.severity, fields, notification.link)
        return "discord", message_id, None

    raise ValueError(f"Unknown channel {delivery.channel}")


def _finalise(notification: Notification, deliveries: List[NotificationDelivery]) -> None:
    external = [d for d in deliveries if d.channel in EXTERNAL_CHANNELS]
    if not external:
        notification.dispatch_status = "DELIVERED"
    elif all(d.status in ("SENT", "SKIPPED") for d in external):
        notification.dispatch_status = "DELIVERED"
    elif any(d.status == "SENT" for d in external):
        notification.dispatch_status = "PARTIAL"
    elif any(d.status in ("PENDING", "FAILED") and (d.attempts or 0) < MAX_ATTEMPTS for d in external):
        notification.dispatch_status = "QUEUED"
    else:
        notification.dispatch_status = "FAILED"
    if notification.dispatch_status in ("DELIVERED", "PARTIAL") and not notification.delivered_at:
        notification.delivered_at = datetime.datetime.utcnow()


def deliver(notification_id: str) -> dict:
    """
    Send every outstanding delivery of one notification.

    Returns ``{"status", "retry", "deliveries"}``. ``retry`` is True when a
    channel failed in a way worth retrying — the QStash endpoint turns that
    into a 503 so QStash tries again later.
    """
    lock = f"deliver-lock:{notification_id}"
    if not cache.set_once(lock, 90):
        return {"status": "IN_PROGRESS", "retry": False, "deliveries": []}

    db = SessionLocal()
    try:
        notification = db.get(Notification, notification_id)
        if not notification:
            return {"status": "MISSING", "retry": False, "deliveries": []}

        deliveries = (
            db.query(NotificationDelivery)
            .filter(NotificationDelivery.notification_id == notification_id)
            .all()
        )
        retry = False
        for delivery in deliveries:
            if delivery.channel not in EXTERNAL_CHANNELS or delivery.status not in ("PENDING", "FAILED"):
                continue
            if (delivery.attempts or 0) >= MAX_ATTEMPTS:
                continue
            delivery.attempts = (delivery.attempts or 0) + 1
            try:
                provider, message_id, note = _send(db, notification, delivery)
                delivery.status, delivery.provider = "SENT", provider
                delivery.provider_message_id = message_id
                delivery.sent_at = datetime.datetime.utcnow()
                delivery.last_error = note
                log_event(
                    "notification delivered",
                    notification_id=notification_id,
                    channel=delivery.channel,
                    provider=provider,
                    event_type=notification.event_type,
                )
            except Exception as exc:
                delivery.status = "FAILED"
                delivery.last_error = str(exc)[:600]
                attempts = getattr(exc, "attempts", None)
                if attempts:
                    delivery.provider = ",".join(a["provider"] for a in attempts)
                if getattr(exc, "retryable", True) and delivery.attempts < MAX_ATTEMPTS:
                    retry = True
                log_event(
                    "notification delivery failed",
                    notification_id=notification_id,
                    channel=delivery.channel,
                    error=str(exc)[:300],
                )
            db.commit()

        _finalise(notification, deliveries)
        db.commit()
        return {
            "status": notification.dispatch_status,
            "retry": retry,
            "deliveries": [
                {"channel": d.channel, "status": d.status, "provider": d.provider} for d in deliveries
            ],
        }
    finally:
        db.close()
        cache.release(lock)


def mark_dead_letter(db: Session, notification_id: str, reason: str) -> Optional[Notification]:
    """QStash gave up after its retries: make the loss loud, not silent."""
    notification = db.get(Notification, notification_id)
    if not notification:
        return None
    notification.dispatch_status = "DEAD_LETTER"
    db.commit()
    log_event("notification dead-lettered", notification_id=notification_id, reason=reason[:300])

    try:
        from cloud import aws

        aws.publish_alert(
            f"PlateletIQ: notification {notification_id} dead-lettered",
            f"{notification.title}\n\nEvent: {notification.event_type}\nFacility: {notification.bank_id}\n"
            f"Reason: {reason}\n\nQStash exhausted its retries. Inspect the delivery log in the console.",
        )
    except Exception as exc:
        logger.warning(f"Could not raise dead-letter alert: {exc!r}")
    return notification


def redrive(db: Session, older_than_minutes: int = 10) -> int:
    """Re-dispatch notifications stuck with undelivered channels (the outbox sweep)."""
    now = datetime.datetime.utcnow()
    candidates = (
        db.query(Notification)
        .filter(
            Notification.dispatch_status.in_(["QUEUED", "FAILED", "PARTIAL", "PENDING"]),
            Notification.created_at <= now - datetime.timedelta(minutes=older_than_minutes),
            Notification.created_at >= now - datetime.timedelta(hours=24),
        )
        .limit(50)
        .all()
    )
    stuck = []
    for notification in candidates:
        outstanding = (
            db.query(NotificationDelivery)
            .filter(
                NotificationDelivery.notification_id == notification.id,
                NotificationDelivery.channel.in_(EXTERNAL_CHANNELS),
                NotificationDelivery.status.in_(["PENDING", "FAILED"]),
                NotificationDelivery.attempts < MAX_ATTEMPTS,
            )
            .count()
        )
        if outstanding:
            stuck.append(notification)
    if stuck:
        dispatch(db, stuck, attempt_suffix=f":r{now.strftime('%Y%m%d%H%M')}")
    return len(stuck)


# ── Read side ────────────────────────────────────────────────────────────────

def serialise(notification: Notification, include_deliveries: bool = True, db: Optional[Session] = None) -> dict:
    payload = {
        "id": notification.id,
        "bank_id": notification.bank_id,
        "event_type": notification.event_type,
        "severity": notification.severity,
        "title": notification.title,
        "body": notification.body,
        "transfer_id": notification.transfer_id,
        "link": notification.link,
        "details": notification.data.get("details") or [],
        "certificate_url": notification.data.get("certificate_url"),
        "dispatch_via": notification.dispatch_via,
        "dispatch_status": notification.dispatch_status,
        "created_at": notification.created_at.isoformat() if notification.created_at else None,
        "read_at": notification.read_at.isoformat() if notification.read_at else None,
    }
    if include_deliveries and db is not None:
        payload["deliveries"] = [
            {
                "id": d.id,
                "channel": d.channel,
                "target": d.target_label,
                "status": d.status,
                "provider": d.provider,
                "attempts": d.attempts,
                "error": d.last_error,
                "sent_at": d.sent_at.isoformat() if d.sent_at else None,
            }
            for d in db.query(NotificationDelivery)
            .filter(NotificationDelivery.notification_id == notification.id)
            .order_by(NotificationDelivery.created_at.asc())
            .all()
        ]
    return payload
