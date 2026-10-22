"""
Courier webhook intake.

Shiprocket posts scan updates here. Each delivery is matched back to a
``Transfer`` by AWB, shipment id, or order id, and drives that transfer's
timeline and last-known position.

What a webhook may **not** do is complete a transfer. A courier scan reading
"DELIVERED" moves the row to ARRIVED and asks the receiving facility for its
code; only the receiver redeeming that code settles the units. Otherwise a
spoofed or mistaken scan could move stock between two ledgers on its own.
"""

import datetime
import hashlib
import hmac
import json
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from cloud.config import env
from database import get_db
from models.shipment import TrackingEvent
from models.transfer import Transfer, TransferEvent
from services.transfer_notifications import broadcast_change, notify_transfer

webhooks_router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SHIPROCKET_WEBHOOK_TOKEN = os.getenv("SHIPROCKET_WEBHOOK_TOKEN", "")

# Courier vocabulary -> our state machine. Anything unmapped is recorded on the
# timeline but leaves the transfer's status alone.
STATUS_MAP = {
    "PICKED UP": "IN_TRANSIT",
    "PICKUP DONE": "IN_TRANSIT",
    "IN TRANSIT": "IN_TRANSIT",
    "IN_TRANSIT": "IN_TRANSIT",
    "SHIPPED": "IN_TRANSIT",
    "OUT FOR DELIVERY": "IN_TRANSIT",
    "REACHED DESTINATION HUB": "IN_TRANSIT",
    "DELIVERED": "ARRIVED",
    "COMPLETED": "ARRIVED",
}


def _authorise(request: Request, body: bytes) -> None:
    """
    Reject a delivery that is not from the configured courier.

    If the header looks like an HMAC signature we verify it against the shared
    secret; otherwise we compare the shared token in constant time. When no
    token is configured the endpoint refuses everything rather than trusting
    anonymous callers — an open webhook can rewrite a shipment's timeline.
    """
    if not SHIPROCKET_WEBHOOK_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Webhook intake is disabled: SHIPROCKET_WEBHOOK_TOKEN is not configured.",
        )

    signature = request.headers.get("x-shiprocket-signature")
    if signature:
        expected = hmac.new(
            SHIPROCKET_WEBHOOK_TOKEN.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected, signature.strip().lower()):
            return
        raise HTTPException(status_code=401, detail="Webhook signature did not verify.")

    token = request.headers.get("x-shiprocket-secret") or request.headers.get("x-webhook-token") or ""
    if not hmac.compare_digest(token, SHIPROCKET_WEBHOOK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing webhook token.")


def _coordinate(payload: dict, scan: dict):
    for candidate in (scan, payload):
        lat = candidate.get("lat") or candidate.get("latitude")
        lng = candidate.get("lng") or candidate.get("long") or candidate.get("longitude")
        try:
            if lat is not None and lng is not None:
                return float(lat), float(lng)
        except (TypeError, ValueError):
            continue
    return None, None


@webhooks_router.post("/shiprocket")
@webhooks_router.post("/logistics-events")
async def shiprocket_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    _authorise(request, body)

    payload_hash = hashlib.sha256(body).hexdigest()
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not valid JSON.")

    event_id = str(payload.get("event_id") or f"EVT-{payload_hash[:16]}")

    # Couriers retry; the same scan must not append twice.
    if db.query(TrackingEvent).filter(TrackingEvent.provider_event_id == event_id).first():
        return {"status": "ok", "deduplicated": True}

    scans = payload.get("scans") or []
    scan = scans[-1] if isinstance(scans, list) and scans else {}
    raw_status = str(payload.get("current_status") or payload.get("status") or scan.get("status") or "").upper()
    awb = payload.get("awb") or payload.get("awb_code")
    shipment_ref = payload.get("shipment_id") or payload.get("order_id")
    lat, lng = _coordinate(payload, scan if isinstance(scan, dict) else {})
    location_name = (scan or {}).get("location") or payload.get("current_location")
    now = datetime.datetime.utcnow()

    transfer = None
    refs = [str(r) for r in (awb, shipment_ref) if r]
    if refs:
        transfer = (
            db.query(Transfer)
            .filter(
                or_(
                    Transfer.awb_code.in_(refs),
                    Transfer.provider_shipment_id.in_(refs),
                    Transfer.provider_order_id.in_(refs),
                )
            )
            .first()
        )

    db.add(
        TrackingEvent(
            id=f"TRK-{uuid.uuid4().hex[:10].upper()}",
            shipment_id=transfer.id if transfer else str(shipment_ref or awb or "UNMATCHED"),
            provider="shiprocket",
            provider_event_id=event_id,
            status=raw_status or "UPDATE",
            description=(scan or {}).get("activity") or payload.get("activity") or raw_status,
            latitude=lat,
            longitude=lng,
            location_name=location_name,
            event_timestamp=now,
            received_at=now,
            raw_payload_hash=payload_hash,
        )
    )

    if not transfer:
        db.commit()
        # 200, not 4xx: a courier retrying forever over an unknown reference
        # helps nobody. The event is stored for reconciliation.
        return {"status": "ok", "matched": False, "reference": awb or shipment_ref}

    if lat is not None and lng is not None:
        transfer.last_lat = lat
        transfer.last_lng = lng
        transfer.last_location_at = now
        transfer.last_location_source = "shiprocket"

    mapped = STATUS_MAP.get(raw_status)
    status_changed = False

    if mapped == "IN_TRANSIT" and transfer.status in ("PICKUP_OTP_REQUIRED", "SHIPMENT_CREATED", "AWB_ASSIGNED"):
        # The rider scanned the pickup before the sending facility confirmed on
        # screen. Record it; the sender's code is still what releases custody.
        pass
    elif mapped == "ARRIVED" and transfer.status == "IN_TRANSIT":
        transfer.arrived_at = now
        transfer.touch("ARRIVED")
        status_changed = True
        db.add(
            TransferEvent(
                id=str(uuid.uuid4()),
                transfer_id=transfer.id,
                status="ARRIVED",
                title="Courier reached the destination",
                description=(
                    f"{location_name or 'Destination'} scan received. "
                    "Waiting for the receiving facility to enter the sender's receipt code."
                ),
                latitude=lat,
                longitude=lng,
                location_name=location_name,
                source="shiprocket",
                occurred_at=now,
            )
        )
    else:
        transfer.touch()

    db.add(
        TransferEvent(
            id=str(uuid.uuid4()),
            transfer_id=transfer.id,
            status=transfer.status,
            title=f"Courier scan: {raw_status.title() or 'update'}",
            description=(scan or {}).get("activity") or location_name,
            latitude=lat,
            longitude=lng,
            location_name=location_name,
            source="shiprocket",
            occurred_at=now,
        )
    )
    db.commit()

    if status_changed and transfer.status == "ARRIVED":
        notify_transfer(db, transfer, "arrived")
    else:
        broadcast_change(transfer)

    return {
        "status": "ok",
        "matched": True,
        "transfer_id": transfer.id,
        "transfer_status": transfer.status,
        "status_changed": status_changed,
    }


# ── Telegram bot ─────────────────────────────────────────────────────────────

@webhooks_router.post("/telegram")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Telegram delivers bot updates here (registered by
    ``scripts/setup_telegram_webhook.py``). Telegram echoes the secret we
    registered in ``X-Telegram-Bot-Api-Secret-Token``; anything without it is
    refused, so nobody else can impersonate a chat linking to a facility.
    """
    secret = env("TELEGRAM_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Telegram webhook is disabled: TELEGRAM_WEBHOOK_SECRET is not set.")
    supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(supplied, secret):
        raise HTTPException(status_code=401, detail="Invalid Telegram secret token.")

    from services.telegram_links import process_update

    try:
        update = json.loads((await request.body()) or b"{}")
        process_update(db, update)
    except Exception as exc:
        # Always 200: a non-2xx makes Telegram redeliver the same update forever.
        db.rollback()
        print(f"Telegram update could not be processed: {exc!r}")
    return {"ok": True}
