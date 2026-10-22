"""
Facility-to-facility transfer pipeline.

Roles are decided by the transfer row, never by the caller's claim:

  * ``source_bank_id``      — the SENDER. Accepts or declines, reserves units,
                              issues both OTPs, confirms pickup.
  * ``destination_bank_id`` — the RECEIVER. Opens a shortage pull, accepts a
                              wastage offer, and enters the delivery OTP.

Every mutating endpoint checks the caller's facility against the row and
returns 403 otherwise, so the two-console handshake is enforced server side.
"""

import datetime
import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models.audit import AuditLog
from models.bank import Bank
from models.transfer import TERMINAL_STATES, Transfer, TransferEvent
from services import inventory_ledger
from services.courier_tracking import remaining_minutes, resolve_location
from services.inventory_ledger import InsufficientStock
from services.otp_service import (
    OTPError,
    generate_otp_challenge,
    get_active_challenge,
    verify_otp_challenge,
)
from services.routing import get_route
from services.transport.base import TransportError
from services.transport.factory import get_named_provider, get_transport_provider
from services.notifications import background
from services.transfer_notifications import broadcast_change, notify_transfer, settle_followup

router = APIRouter(tags=["transfers"])

COLD_CHAIN_INSTRUCTIONS = [
    "Medical cargo — perishable platelet concentrate.",
    "Keep upright and agitated at 20–24 °C.",
    "DO NOT REFRIGERATE and do not pack with ice or gel packs.",
    "Hand over only against the recipient's one-time code.",
]


# ── Request bodies ───────────────────────────────────────────────────────────

class CreateTransferRequest(BaseModel):
    counterparty_bank_id: str = Field(..., description="The other facility in the transfer.")
    direction: str = Field("SHORTAGE_PULL", description="SHORTAGE_PULL | WASTAGE_PUSH")
    units: int = Field(1, ge=1, le=200)
    component_type: str = Field("SDP", description="SDP | RDP")
    blood_group: Optional[str] = None
    priority: str = Field("URGENT", description="ROUTINE | URGENT | EMERGENCY")
    reason: Optional[str] = None
    provider: Optional[str] = None


class DeclineRequest(BaseModel):
    reason: Optional[str] = None


class OTPVerifyRequest(BaseModel):
    otp: str


# ── Helpers ──────────────────────────────────────────────────────────────────

def _caller_bank_id(current_user: dict) -> str:
    bank_id = current_user.get("bank_id")
    if not bank_id:
        raise HTTPException(
            status_code=403,
            detail="No facility selected for this session. Choose a facility before acting on transfers.",
        )
    return bank_id


def _get_bank(db: Session, bank_id: str) -> Bank:
    bank = db.query(Bank).filter(Bank.id == bank_id).first()
    if not bank:
        raise HTTPException(status_code=404, detail=f"Facility {bank_id} is not in the registry.")
    return bank


def _get_transfer(db: Session, transfer_id: str) -> Transfer:
    transfer = db.query(Transfer).filter(Transfer.id == transfer_id).first()
    if not transfer:
        raise HTTPException(status_code=404, detail=f"Transfer {transfer_id} not found.")
    return transfer


def _require_sender(transfer: Transfer, bank_id: str) -> None:
    if transfer.source_bank_id != bank_id:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Only the sending facility ({transfer.source_bank_id}) can perform this step. "
                f"You are signed in as {bank_id}."
            ),
        )


def _require_receiver(transfer: Transfer, bank_id: str) -> None:
    if transfer.destination_bank_id != bank_id:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Only the receiving facility ({transfer.destination_bank_id}) can perform this step. "
                f"You are signed in as {bank_id}."
            ),
        )


def _require_party(transfer: Transfer, bank_id: str) -> None:
    if bank_id not in (transfer.source_bank_id, transfer.destination_bank_id):
        raise HTTPException(status_code=403, detail="Your facility is not a party to this transfer.")


def _log_event(
    db: Session,
    transfer: Transfer,
    status: str,
    title: str,
    description: str = None,
    actor_bank_id: str = None,
    actor_user_id: str = None,
    location: Dict[str, Any] = None,
    source: str = "system",
) -> None:
    db.add(
        TransferEvent(
            id=str(uuid.uuid4()),
            transfer_id=transfer.id,
            status=status,
            title=title,
            description=description,
            actor_bank_id=actor_bank_id,
            actor_user_id=actor_user_id,
            latitude=(location or {}).get("lat"),
            longitude=(location or {}).get("lng"),
            location_name=(location or {}).get("name"),
            source=source,
            occurred_at=datetime.datetime.utcnow(),
        )
    )


def _audit(db: Session, current_user: dict, bank_id: str, action: str, entity_id: str) -> None:
    db.add(
        AuditLog(
            id=str(uuid.uuid4()),
            actor_user_id=current_user.get("sub", "unknown"),
            bank_id=bank_id,
            action=action,
            entity_type="Transfer",
            entity_id=entity_id,
        )
    )


def _serialise(db: Session, transfer: Transfer, viewer_bank_id: str = None) -> Dict[str, Any]:
    """Shape a transfer for the client, from that viewer's point of view."""
    source = db.query(Bank).filter(Bank.id == transfer.source_bank_id).first()
    destination = db.query(Bank).filter(Bank.id == transfer.destination_bank_id).first()

    role = None
    if viewer_bank_id == transfer.source_bank_id:
        role = "SENDER"
    elif viewer_bank_id == transfer.destination_bank_id:
        role = "RECEIVER"

    pickup_challenge = get_active_challenge(db, transfer.id, "PICKUP")
    delivery_challenge = get_active_challenge(db, transfer.id, "DELIVERY")

    return {
        "id": transfer.id,
        "direction": transfer.direction,
        "status": transfer.status,
        "revision": transfer.revision,
        "viewer_role": role,
        "units": transfer.units,
        "component_type": transfer.component_type,
        "blood_group": transfer.blood_group,
        "priority": transfer.priority,
        "reason": transfer.reason,
        "decline_reason": transfer.decline_reason,
        "opened_by_bank_id": transfer.opened_by_bank_id,
        "source": source.as_dict() if source else {"id": transfer.source_bank_id},
        "destination": destination.as_dict() if destination else {"id": transfer.destination_bank_id},
        "distance_km": transfer.distance_km,
        "eta_minutes": transfer.eta_minutes,
        "eta_remaining_minutes": remaining_minutes(transfer),
        "route_provider": transfer.route_provider,
        "route_geometry": transfer.route_geometry,
        "transport_provider": transfer.transport_provider,
        "transport_provider_label": transfer.provider_label,
        "awb_code": transfer.awb_code,
        "courier_name": transfer.courier_name,
        "rider": {
            "name": transfer.rider_name,
            "mobile": transfer.rider_mobile,
            "vehicle": transfer.vehicle_number,
        } if transfer.rider_name else None,
        "instructions": COLD_CHAIN_INSTRUCTIONS,
        "reserved_units": len(transfer.reserved_unit_ids),
        "certificate": {
            "available": transfer.status == "TRANSFER_COMPLETED",
            "archived_in": transfer.certificate_store,
            "sha256": transfer.certificate_sha256,
        },
        # Never the codes themselves — only whether one is live and who owes it.
        "pickup_otp": {
            "issued": pickup_challenge is not None,
            "expires_at": pickup_challenge.expires_at.isoformat() if pickup_challenge else None,
            "verifier_bank_id": pickup_challenge.verifier_bank_id if pickup_challenge else None,
        },
        "delivery_otp": {
            "issued": delivery_challenge is not None,
            "expires_at": delivery_challenge.expires_at.isoformat() if delivery_challenge else None,
            "verifier_bank_id": delivery_challenge.verifier_bank_id if delivery_challenge else None,
        },
        "created_at": transfer.created_at.isoformat() if transfer.created_at else None,
        "accepted_at": transfer.accepted_at.isoformat() if transfer.accepted_at else None,
        "dispatched_at": transfer.dispatched_at.isoformat() if transfer.dispatched_at else None,
        "completed_at": transfer.completed_at.isoformat() if transfer.completed_at else None,
        "updated_at": transfer.updated_at.isoformat() if transfer.updated_at else None,
    }


# ── Create ───────────────────────────────────────────────────────────────────

@router.post("/transfers")
def create_transfer(
    req: CreateTransferRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Open a transfer against another facility.

    ``SHORTAGE_PULL``: the caller is short and asks the counterparty to send.
    ``WASTAGE_PUSH``:  the caller holds units near expiry and offers them.

    The route is computed immediately from both facilities' stored coordinates
    so the counterparty sees a real distance and ETA before deciding.
    """
    caller_bank_id = _caller_bank_id(current_user)

    if req.counterparty_bank_id == caller_bank_id:
        raise HTTPException(status_code=400, detail="A facility cannot transfer to itself.")

    direction = req.direction.upper()
    if direction not in ("SHORTAGE_PULL", "WASTAGE_PUSH"):
        raise HTTPException(status_code=400, detail="direction must be SHORTAGE_PULL or WASTAGE_PUSH.")

    if direction == "SHORTAGE_PULL":
        source_id, destination_id = req.counterparty_bank_id, caller_bank_id
    else:
        source_id, destination_id = caller_bank_id, req.counterparty_bank_id

    source = _get_bank(db, source_id)
    destination = _get_bank(db, destination_id)

    # A wastage push is the caller promising their own stock, so check it now.
    # A shortage pull is checked when the sender accepts, since only they can
    # see their own usable pool at that moment.
    if direction == "WASTAGE_PUSH":
        available = inventory_ledger.count_available(db, source_id, req.component_type, req.blood_group)
        if available < req.units:
            raise HTTPException(
                status_code=400,
                detail=f"Your facility holds {available} usable {req.component_type} unit(s); {req.units} offered.",
            )

    if None in (source.latitude, source.longitude, destination.latitude, destination.longitude):
        raise HTTPException(
            status_code=400,
            detail="One of the facilities has no coordinates on record, so no route can be planned.",
        )

    route = get_route(source.latitude, source.longitude, destination.latitude, destination.longitude)

    transfer = Transfer(
        id=f"TRF-{uuid.uuid4().hex[:8].upper()}",
        direction=direction,
        source_bank_id=source_id,
        destination_bank_id=destination_id,
        opened_by_bank_id=caller_bank_id,
        opened_by_user_id=current_user.get("sub"),
        component_type=req.component_type,
        blood_group=req.blood_group,
        units=req.units,
        priority=req.priority,
        reason=req.reason,
        status="REQUESTED",
        distance_km=route["distance_km"],
        eta_minutes=route["duration_min"],
        route_geometry_json=json.dumps(route["route_geometry"]),
        route_provider=route["provider"],
        transport_provider=req.provider,
        revision=1,
    )
    db.add(transfer)

    opener = source if caller_bank_id == source_id else destination
    counterparty = destination if caller_bank_id == source_id else source
    verb = "offered" if direction == "WASTAGE_PUSH" else "requested"

    _log_event(
        db, transfer, "REQUESTED",
        f"{req.units} {req.component_type} unit(s) {verb}",
        f"{opener.name} {verb} {req.units} {req.component_type} unit(s) "
        f"{'to' if direction == 'WASTAGE_PUSH' else 'from'} {counterparty.name}. "
        f"Routed distance {route['distance_km']} km, ETA {route['duration_min']} min.",
        actor_bank_id=caller_bank_id,
        actor_user_id=current_user.get("sub"),
        location={"lat": source.latitude, "lng": source.longitude, "name": source.name},
    )
    _audit(db, current_user, caller_bank_id, f"OPEN_TRANSFER_{direction}", transfer.id)
    db.commit()
    db.refresh(transfer)
    notify_transfer(db, transfer, "requested")

    return {"data": _serialise(db, transfer, caller_bank_id), "error": None}


# ── Sender decisions ─────────────────────────────────────────────────────────

@router.post("/transfers/{transfer_id}/accept")
def accept_transfer(
    transfer_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Sender authorises the transfer.

    This is the single step that reserves stock, books the courier, and issues
    the pickup code. If the courier booking fails the reservation is rolled
    back, so units are never stranded by a logistics outage.
    """
    caller_bank_id = _caller_bank_id(current_user)
    transfer = _get_transfer(db, transfer_id)
    _require_sender(transfer, caller_bank_id)

    if transfer.status != "REQUESTED":
        raise HTTPException(
            status_code=409,
            detail=f"Transfer is {transfer.status}; only a REQUESTED transfer can be accepted.",
        )

    source = _get_bank(db, transfer.source_bank_id)
    destination = _get_bank(db, transfer.destination_bank_id)

    # 1. Lock the stock, nearest expiry first.
    try:
        reserved_ids = inventory_ledger.reserve_units(
            db,
            bank_id=transfer.source_bank_id,
            component_type=transfer.component_type,
            quantity=transfer.units,
            transfer_id=transfer.id,
            actor_user_id=current_user.get("sub", "unknown"),
            blood_group=transfer.blood_group,
        )
    except InsufficientStock as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    transfer.reserved_unit_ids_json = json.dumps(reserved_ids)
    transfer.accepted_at = datetime.datetime.utcnow()
    transfer.touch("UNITS_RESERVED")

    _log_event(
        db, transfer, "ACCEPTED", "Request authorised",
        f"{source.name} authorised the transfer.",
        actor_bank_id=caller_bank_id, actor_user_id=current_user.get("sub"),
    )
    _log_event(
        db, transfer, "UNITS_RESERVED", f"{len(reserved_ids)} unit(s) reserved",
        f"Units locked out of {source.name}'s usable pool, nearest expiry first.",
        actor_bank_id=caller_bank_id,
    )
    db.commit()

    # 2. Book the courier.
    try:
        provider_key, provider = get_named_provider(db, transfer.transport_provider or "")
    except TransportError as exc:
        raise HTTPException(status_code=400, detail=exc.message)

    try:
        order = provider.create_order(
            transfer_id=transfer.id,
            pickup_address=source.address or source.name,
            pickup_lat=source.latitude,
            pickup_lng=source.longitude,
            pickup_name=source.short_name or source.name,
            pickup_mobile=source.phone or "",
            drop_address=destination.address or destination.name,
            drop_lat=destination.latitude,
            drop_lng=destination.longitude,
            drop_name=destination.short_name or destination.name,
            drop_mobile=destination.phone or "",
            units_count=transfer.units,
            pickup_pincode=source.pincode or "",
            drop_pincode=destination.pincode or "",
        )
    except TransportError as exc:
        # Roll the reservation back so the units stay usable.
        inventory_ledger.release_units(
            db, reserved_ids, transfer.id, current_user.get("sub", "unknown"),
            "Courier dispatch failed — reservation rolled back",
        )
        transfer.reserved_unit_ids_json = json.dumps([])
        transfer.decline_reason = exc.message
        transfer.touch("FAILED")
        _log_event(
            db, transfer, "FAILED", "Courier dispatch failed",
            f"{exc.message} Units were returned to {source.name}'s usable pool.",
            actor_bank_id=caller_bank_id, source="courier",
        )
        _audit(db, current_user, caller_bank_id, "TRANSFER_DISPATCH_FAILED", transfer.id)
        db.commit()
        notify_transfer(db, transfer, "dispatch_failed")
        raise HTTPException(status_code=502, detail=f"Courier dispatch failed: {exc.message}")

    # Store the canonical key so this row always resolves back to the same
    # adapter; the adapter's self-reported label goes on the courier name.
    transfer.transport_provider = provider_key
    transfer.provider_label = order.get("provider")
    transfer.provider_order_id = order.get("order_id")
    transfer.provider_shipment_id = order.get("shipment_id")
    transfer.awb_code = order.get("awb_code")
    transfer.courier_name = order.get("courier_name")
    transfer.rider_name = order.get("driver_name")
    transfer.rider_mobile = order.get("driver_mobile")
    transfer.vehicle_number = order.get("vehicle_number")
    transfer.touch("SHIPMENT_CREATED")

    _log_event(
        db, transfer, "SHIPMENT_CREATED", "Courier booked",
        f"{transfer.courier_name or 'Courier'} order {transfer.provider_order_id} created "
        f"via {transfer.provider_label or provider_key}.",
        actor_bank_id=caller_bank_id, source="courier",
        location={"lat": source.latitude, "lng": source.longitude, "name": source.name},
    )
    if transfer.awb_code:
        transfer.touch("AWB_ASSIGNED")
        _log_event(
            db, transfer, "AWB_ASSIGNED", f"Airway bill {transfer.awb_code}",
            f"Track this shipment against AWB {transfer.awb_code}.",
            source="courier",
        )
    db.commit()

    # 3. Issue the pickup code for the rider at the sender's door.
    try:
        _, pickup_code, expires_at = generate_otp_challenge(
            db,
            transfer_id=transfer.id,
            purpose="PICKUP",
            issued_by_bank_id=transfer.source_bank_id,
            verifier_bank_id=transfer.source_bank_id,
            shipment_id=transfer.provider_shipment_id,
            created_by=current_user.get("sub", "unknown"),
        )
    except OTPError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    transfer.touch("PICKUP_OTP_REQUIRED")
    _log_event(
        db, transfer, "PICKUP_OTP_REQUIRED", "Pickup code issued",
        f"{source.name} holds a one-time pickup code. Confirm it when the rider collects the box.",
        actor_bank_id=caller_bank_id,
    )
    _audit(db, current_user, caller_bank_id, "ACCEPT_TRANSFER", transfer.id)
    db.commit()
    db.refresh(transfer)
    notify_transfer(db, transfer, "accepted")

    payload = _serialise(db, transfer, caller_bank_id)
    # Returned exactly once, to the facility that issued it.
    payload["pickup_otp_code"] = pickup_code
    payload["pickup_otp_expires_at"] = expires_at.isoformat()
    return {"data": payload, "error": None}


@router.post("/transfers/{transfer_id}/decline")
def decline_transfer(
    transfer_id: str,
    req: DeclineRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Sender refuses an open request."""
    caller_bank_id = _caller_bank_id(current_user)
    transfer = _get_transfer(db, transfer_id)
    _require_sender(transfer, caller_bank_id)

    if transfer.status != "REQUESTED":
        raise HTTPException(status_code=409, detail=f"Transfer is {transfer.status}; it cannot be declined.")

    transfer.decline_reason = req.reason
    transfer.touch("DECLINED")
    _log_event(
        db, transfer, "DECLINED", "Request declined",
        req.reason or "No reason given.",
        actor_bank_id=caller_bank_id, actor_user_id=current_user.get("sub"),
    )
    _audit(db, current_user, caller_bank_id, "DECLINE_TRANSFER", transfer.id)
    db.commit()
    db.refresh(transfer)
    notify_transfer(db, transfer, "declined")
    return {"data": _serialise(db, transfer, caller_bank_id), "error": None}


@router.post("/transfers/{transfer_id}/cancel")
def cancel_transfer(
    transfer_id: str,
    req: DeclineRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Whoever opened the transfer withdraws it, before the box leaves."""
    caller_bank_id = _caller_bank_id(current_user)
    transfer = _get_transfer(db, transfer_id)

    if transfer.opened_by_bank_id != caller_bank_id:
        raise HTTPException(status_code=403, detail="Only the facility that opened this transfer can cancel it.")
    if transfer.status in TERMINAL_STATES:
        raise HTTPException(status_code=409, detail=f"Transfer is already {transfer.status}.")
    if transfer.status in ("IN_TRANSIT", "ARRIVED", "DELIVERY_OTP_REQUIRED"):
        raise HTTPException(
            status_code=409,
            detail="The shipment is already with the courier and can no longer be cancelled here.",
        )

    released = inventory_ledger.release_units(
        db, transfer.reserved_unit_ids, transfer.id,
        current_user.get("sub", "unknown"), "Transfer cancelled by opener",
    )
    transfer.reserved_unit_ids_json = json.dumps([])
    transfer.decline_reason = req.reason
    transfer.touch("CANCELLED")
    _log_event(
        db, transfer, "CANCELLED", "Transfer cancelled",
        f"{req.reason or 'Withdrawn by the opening facility.'} {released} unit(s) released.",
        actor_bank_id=caller_bank_id, actor_user_id=current_user.get("sub"),
    )
    _audit(db, current_user, caller_bank_id, "CANCEL_TRANSFER", transfer.id)
    db.commit()
    db.refresh(transfer)
    notify_transfer(db, transfer, "cancelled")
    return {"data": _serialise(db, transfer, caller_bank_id), "error": None}


# ── Pickup: sender issues, sender confirms, custody moves to the courier ─────

@router.post("/transfers/{transfer_id}/pickup/reissue")
def reissue_pickup_otp(
    transfer_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Sender asks for a fresh pickup code (previous one expired or was lost)."""
    caller_bank_id = _caller_bank_id(current_user)
    transfer = _get_transfer(db, transfer_id)
    _require_sender(transfer, caller_bank_id)

    if transfer.status not in ("SHIPMENT_CREATED", "AWB_ASSIGNED", "PICKUP_OTP_REQUIRED", "UNITS_RESERVED"):
        raise HTTPException(status_code=409, detail=f"Transfer is {transfer.status}; pickup is not pending.")

    try:
        _, code, expires_at = generate_otp_challenge(
            db, transfer_id=transfer.id, purpose="PICKUP",
            issued_by_bank_id=transfer.source_bank_id,
            verifier_bank_id=transfer.source_bank_id,
            created_by=current_user.get("sub", "unknown"),
        )
    except OTPError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    transfer.touch("PICKUP_OTP_REQUIRED")
    _log_event(db, transfer, "PICKUP_OTP_REQUIRED", "Pickup code re-issued",
               "The previous pickup code was superseded.", actor_bank_id=caller_bank_id)
    db.commit()
    broadcast_change(transfer)

    return {
        "data": {"transfer_id": transfer.id, "otp_code": code, "expires_at": expires_at.isoformat()},
        "error": None,
    }


@router.post("/transfers/{transfer_id}/pickup/verify")
def verify_pickup(
    transfer_id: str,
    req: OTPVerifyRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Sender confirms the rider has the box.

    On success custody moves to the courier and the **delivery** code is
    issued — also by the sender, but verifiable only by the receiver.
    """
    caller_bank_id = _caller_bank_id(current_user)
    transfer = _get_transfer(db, transfer_id)
    _require_sender(transfer, caller_bank_id)

    if transfer.status not in ("PICKUP_OTP_REQUIRED", "SHIPMENT_CREATED", "AWB_ASSIGNED"):
        raise HTTPException(status_code=409, detail=f"Transfer is {transfer.status}; pickup is not pending.")

    result = verify_otp_challenge(
        db, transfer_id=transfer.id, purpose="PICKUP",
        code=req.otp, verifying_bank_id=caller_bank_id,
    )
    if not result["valid"]:
        raise HTTPException(status_code=400, detail=result["reason"])

    source = _get_bank(db, transfer.source_bank_id)
    destination = _get_bank(db, transfer.destination_bank_id)

    transfer.dispatched_at = datetime.datetime.utcnow()
    transfer.touch("IN_TRANSIT")
    _log_event(
        db, transfer, "IN_TRANSIT", "Picked up",
        f"Custody passed to {transfer.courier_name or 'the courier'} at {source.name}. "
        f"{transfer.units} {transfer.component_type} unit(s) en route to {destination.name}.",
        actor_bank_id=caller_bank_id, actor_user_id=current_user.get("sub"),
        location={"lat": source.latitude, "lng": source.longitude, "name": source.name},
    )

    # The delivery code: issued here, by the sender, for the receiver to enter.
    try:
        _, delivery_code, expires_at = generate_otp_challenge(
            db, transfer_id=transfer.id, purpose="DELIVERY",
            issued_by_bank_id=transfer.source_bank_id,
            verifier_bank_id=transfer.destination_bank_id,
            shipment_id=transfer.provider_shipment_id,
            created_by=current_user.get("sub", "unknown"),
        )
    except OTPError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    _log_event(
        db, transfer, "IN_TRANSIT", "Receipt code issued",
        f"{source.name} issued a one-time receipt code for {destination.name}. "
        "Only the receiving facility can redeem it.",
        actor_bank_id=caller_bank_id,
    )
    _audit(db, current_user, caller_bank_id, "PICKUP_VERIFIED", transfer.id)
    db.commit()
    db.refresh(transfer)
    notify_transfer(db, transfer, "picked_up")

    payload = _serialise(db, transfer, caller_bank_id)
    payload["delivery_otp_code"] = delivery_code
    payload["delivery_otp_expires_at"] = expires_at.isoformat()
    payload["delivery_otp_verifier_bank_id"] = transfer.destination_bank_id
    return {"data": payload, "error": None}


# ── Delivery: receiver redeems the sender's code ─────────────────────────────

@router.post("/transfers/{transfer_id}/delivery/reissue")
def reissue_delivery_otp(
    transfer_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Sender issues a fresh receipt code.

    The code is shown once, so an officer who closed the screen before passing
    it on has no way to read it again. Without this the shipment would be
    stranded in transit with units reserved and no way to settle them. Issuing
    a new code supersedes the old one, so only the newest is ever redeemable.
    """
    caller_bank_id = _caller_bank_id(current_user)
    transfer = _get_transfer(db, transfer_id)
    _require_sender(transfer, caller_bank_id)

    if transfer.status not in ("IN_TRANSIT", "ARRIVED", "DELIVERY_OTP_REQUIRED"):
        raise HTTPException(
            status_code=409,
            detail=f"Transfer is {transfer.status}; there is no shipment awaiting receipt.",
        )

    try:
        _, code, expires_at = generate_otp_challenge(
            db, transfer_id=transfer.id, purpose="DELIVERY",
            issued_by_bank_id=transfer.source_bank_id,
            verifier_bank_id=transfer.destination_bank_id,
            shipment_id=transfer.provider_shipment_id,
            created_by=current_user.get("sub", "unknown"),
        )
    except OTPError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    transfer.touch()
    _log_event(
        db, transfer, transfer.status, "Receipt code re-issued",
        "The previous receipt code was superseded and can no longer be redeemed.",
        actor_bank_id=caller_bank_id, actor_user_id=current_user.get("sub"),
    )
    _audit(db, current_user, caller_bank_id, "REISSUE_DELIVERY_OTP", transfer.id)
    db.commit()
    broadcast_change(transfer)

    return {
        "data": {
            "transfer_id": transfer.id,
            "otp_code": code,
            "expires_at": expires_at.isoformat(),
            "verifier_bank_id": transfer.destination_bank_id,
        },
        "error": None,
    }


@router.post("/transfers/{transfer_id}/delivery/verify")
def verify_delivery(
    transfer_id: str,
    req: OTPVerifyRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Receiving facility redeems the sender's receipt code.

    Redeeming it settles the ledger: the reserved bags change custody to the
    receiver and become usable there. This is the only path that moves stock
    between facilities.
    """
    caller_bank_id = _caller_bank_id(current_user)
    transfer = _get_transfer(db, transfer_id)
    _require_receiver(transfer, caller_bank_id)

    if transfer.status == "TRANSFER_COMPLETED":
        raise HTTPException(status_code=409, detail="This transfer is already settled.")
    if transfer.status not in ("IN_TRANSIT", "ARRIVED", "DELIVERY_OTP_REQUIRED"):
        raise HTTPException(
            status_code=409,
            detail=f"Transfer is {transfer.status}; the shipment has not been picked up yet.",
        )

    result = verify_otp_challenge(
        db, transfer_id=transfer.id, purpose="DELIVERY",
        code=req.otp, verifying_bank_id=caller_bank_id,
    )
    if not result["valid"]:
        raise HTTPException(status_code=400, detail=result["reason"])

    destination = _get_bank(db, transfer.destination_bank_id)
    source = _get_bank(db, transfer.source_bank_id)

    settlement = inventory_ledger.settle_units(
        db,
        unit_ids=transfer.reserved_unit_ids,
        source_bank_id=transfer.source_bank_id,
        destination_bank_id=transfer.destination_bank_id,
        transfer_id=transfer.id,
        actor_user_id=current_user.get("sub", "unknown"),
    )

    now = datetime.datetime.utcnow()
    transfer.arrived_at = transfer.arrived_at or now
    transfer.completed_at = now
    transfer.last_lat = destination.latitude
    transfer.last_lng = destination.longitude
    transfer.last_location_at = now
    transfer.last_location_source = "delivered"
    transfer.touch("TRANSFER_COMPLETED")

    _log_event(
        db, transfer, "TRANSFER_COMPLETED", "Delivered and settled",
        f"{destination.name} redeemed the receipt code. "
        f"{settlement['transferred']} unit(s) moved from {source.name} to {destination.name}.",
        actor_bank_id=caller_bank_id, actor_user_id=current_user.get("sub"),
        location={"lat": destination.latitude, "lng": destination.longitude, "name": destination.name},
    )
    _audit(db, current_user, caller_bank_id, "DELIVERY_VERIFIED_SETTLED", transfer.id)
    db.commit()
    db.refresh(transfer)
    broadcast_change(transfer)
    # Certificate (PDF -> S3 / Supabase Storage) and the "settled" alerts run
    # off the request thread, so the receiver's console returns immediately.
    background(settle_followup, transfer.id)

    payload = _serialise(db, transfer, caller_bank_id)
    payload["settlement"] = {
        "units_transferred": settlement["transferred"],
        "source_bank_id": transfer.source_bank_id,
        "destination_bank_id": transfer.destination_bank_id,
    }
    return {"data": payload, "error": None}


# ── Reads ────────────────────────────────────────────────────────────────────

@router.get("/transfers")
def list_transfers(
    scope: str = Query("all", description="all | incoming | outgoing | active"),
    since_revision: Optional[int] = Query(None, description="Only rows changed past this revision."),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Transfers this facility is a party to.

    ``incoming`` is what needs a decision or a code from you; ``outgoing`` is
    what you opened and are waiting on. Clients poll this and compare
    ``revision`` to spot changes made on the other console.
    """
    caller_bank_id = _caller_bank_id(current_user)

    query = db.query(Transfer).filter(
        or_(Transfer.source_bank_id == caller_bank_id, Transfer.destination_bank_id == caller_bank_id)
    )

    if scope == "incoming":
        # Requests awaiting my decision, plus shipments awaiting my receipt code.
        query = query.filter(
            or_(
                (Transfer.source_bank_id == caller_bank_id) & (Transfer.status == "REQUESTED"),
                (Transfer.destination_bank_id == caller_bank_id)
                & Transfer.status.in_(["IN_TRANSIT", "ARRIVED", "DELIVERY_OTP_REQUIRED"]),
            )
        )
    elif scope == "outgoing":
        query = query.filter(Transfer.opened_by_bank_id == caller_bank_id)
    elif scope == "active":
        query = query.filter(Transfer.status.notin_(list(TERMINAL_STATES)))

    if since_revision is not None:
        query = query.filter(Transfer.revision > since_revision)

    transfers = query.order_by(Transfer.updated_at.desc()).all()

    return {
        "data": [_serialise(db, t, caller_bank_id) for t in transfers],
        "meta": {
            "bank_id": caller_bank_id,
            "scope": scope,
            "count": len(transfers),
            "max_revision": max((t.revision for t in transfers), default=0),
            "server_time": datetime.datetime.utcnow().isoformat(),
        },
        "error": None,
    }


@router.get("/transfers/{transfer_id}")
def get_transfer(
    transfer_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    caller_bank_id = _caller_bank_id(current_user)
    transfer = _get_transfer(db, transfer_id)
    _require_party(transfer, caller_bank_id)
    return {"data": _serialise(db, transfer, caller_bank_id), "error": None}


@router.get("/transfers/{transfer_id}/track")
def track_transfer(
    transfer_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Live tracking payload: route polyline, current position, and the timeline.

    ``location.location_source`` states how the position was obtained, so the
    map can distinguish a courier GPS fix from a route projection instead of
    presenting both as telemetry.
    """
    caller_bank_id = _caller_bank_id(current_user)
    transfer = _get_transfer(db, transfer_id)
    _require_party(transfer, caller_bank_id)

    source = _get_bank(db, transfer.source_bank_id)
    destination = _get_bank(db, transfer.destination_bank_id)

    # Ask the courier for a fresh fix while the box is moving.
    if transfer.status in ("IN_TRANSIT", "ARRIVED") and transfer.provider_order_id:
        try:
            tracked = get_transport_provider(db, transfer.transport_provider or "").track_order(
                transfer.provider_order_id
            )
            fix = tracked.get("location") or {}
            if fix.get("lat") is not None and fix.get("lng") is not None:
                transfer.last_lat = fix["lat"]
                transfer.last_lng = fix["lng"]
                transfer.last_location_at = datetime.datetime.utcnow()
                transfer.last_location_source = "shiprocket"
                db.commit()
        except Exception as exc:
            # A tracking outage must not break the page; the projection covers it.
            print(f"Courier tracking unavailable for {transfer.id}: {exc}")

    location = resolve_location(
        transfer,
        origin={"lat": source.latitude, "lng": source.longitude},
        destination={"lat": destination.latitude, "lng": destination.longitude},
    )

    events = (
        db.query(TransferEvent)
        .filter(TransferEvent.transfer_id == transfer.id)
        .order_by(TransferEvent.occurred_at.asc())
        .all()
    )

    payload = _serialise(db, transfer, caller_bank_id)
    payload["location"] = location
    payload["timeline"] = [
        {
            "status": e.status,
            "title": e.title,
            "description": e.description,
            "actor_bank_id": e.actor_bank_id,
            "location_name": e.location_name,
            "source": e.source,
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
        }
        for e in events
    ]
    return {"data": payload, "error": None}


@router.get("/transfers/{transfer_id}/events")
def transfer_events(
    transfer_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    caller_bank_id = _caller_bank_id(current_user)
    transfer = _get_transfer(db, transfer_id)
    _require_party(transfer, caller_bank_id)

    events = (
        db.query(TransferEvent)
        .filter(TransferEvent.transfer_id == transfer.id)
        .order_by(TransferEvent.occurred_at.asc())
        .all()
    )
    return {
        "data": [
            {
                "status": e.status,
                "title": e.title,
                "description": e.description,
                "actor_bank_id": e.actor_bank_id,
                "source": e.source,
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            }
            for e in events
        ],
        "error": None,
    }


@router.get("/transfers/{transfer_id}/certificate")
def transfer_certificate(
    transfer_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    The chain-of-custody certificate for a settled transfer.

    Archived copies (S3 / Supabase Storage) come back as a fresh 15-minute
    link; otherwise the PDF is rendered on the spot, so the certificate is
    available even with no object storage configured.
    """
    caller_bank_id = _caller_bank_id(current_user)
    transfer = _get_transfer(db, transfer_id)
    _require_party(transfer, caller_bank_id)
    if transfer.status != "TRANSFER_COMPLETED":
        raise HTTPException(status_code=409, detail="A certificate is issued once the transfer has settled.")

    from services.certificate_service import fresh_link, render

    link = fresh_link(transfer)
    if link:
        return {"data": {"url": link, "archived_in": transfer.certificate_store}, "error": None}

    pdf, _ = render(db, transfer)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{transfer.id}-custody-certificate.pdf"'},
    )
