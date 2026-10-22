"""
Who hears about each step of a transfer, and what they are told.

Called from the transfer endpoints *after* their own commit. Everything here
is best-effort: a notification outage is logged and reported to Sentry, but
it can never fail, delay or roll back a custody step.

    step              told                         why
    ───────────────   ──────────────────────────   ─────────────────────────────
    requested         the counterparty             a pull needs the sender to act;
                                                   a push tells the receiver it is coming
    accepted          the receiver                 stock reserved, courier booked
    declined          the opener                   find another source
    cancelled         the counterparty             stop preparing
    dispatch_failed   both                         units were released, retry
    picked_up         the receiver                 box on the road, ETA
    arrived           the receiver                 redeem the receipt code now
    completed         both + certificate link      settled on both ledgers

Every step also reaches the Discord ops channel, and the two that matter to a
regulator (an EMERGENCY request, a failed dispatch) are raised on the AWS
alerts topic.
"""

from typing import Optional

from sqlalchemy.orm import Session

from cloud import realtime
from cloud.observability import capture_exception, logger
from database import SessionLocal
from models.bank import Bank
from models.transfer import Transfer
from services import notifications

PRIORITY_SEVERITY = {"EMERGENCY": "critical", "URGENT": "warning", "ROUTINE": "info"}


def broadcast_change(transfer: Transfer) -> None:
    """Tell both facilities' open consoles that this row changed (Pusher)."""
    if not realtime.configured():
        return
    payload = {"transfer_id": transfer.id, "status": transfer.status, "revision": transfer.revision}
    for bank_id in {transfer.source_bank_id, transfer.destination_bank_id}:
        notifications.background(realtime.publish, bank_id, "transfer.updated", payload)


def _short(bank: Optional[Bank], fallback: str) -> str:
    return (bank.short_name or bank.name) if bank else fallback


def notify_transfer(db: Session, transfer: Transfer, kind: str, extra: Optional[dict] = None) -> None:
    try:
        broadcast_change(transfer)
        _notify(db, transfer, kind, extra or {})
    except Exception as exc:
        logger.exception(f"Transfer notification '{kind}' for {transfer.id} failed: {exc!r}")
        capture_exception(exc)


def _notify(db: Session, transfer: Transfer, kind: str, extra: dict) -> None:
    source = db.get(Bank, transfer.source_bank_id)
    destination = db.get(Bank, transfer.destination_bank_id)
    sender, receiver = _short(source, transfer.source_bank_id), _short(destination, transfer.destination_bank_id)
    opener_is_sender = transfer.opened_by_bank_id == transfer.source_bank_id
    counterparty = transfer.destination_bank_id if opener_is_sender else transfer.source_bank_id

    what = f"{transfer.units} {transfer.component_type} unit(s)" + (
        f" ({transfer.blood_group})" if transfer.blood_group else ""
    )
    details = [
        ("Transfer", transfer.id),
        ("Units", what),
        ("From", sender),
        ("To", receiver),
        ("Priority", transfer.priority),
    ]
    if transfer.distance_km:
        details.append(("Route", f"{transfer.distance_km} km · ~{round(transfer.eta_minutes or 0)} min"))
    if transfer.awb_code:
        details.append(("Airway bill", transfer.awb_code))

    severity = PRIORITY_SEVERITY.get((transfer.priority or "").upper(), "info")
    common = dict(
        event_type=f"TRANSFER_{kind.upper()}",
        transfer_id=transfer.id,
        link=notifications.console_link("transfers", transfer.id),
        data={"details": details},
        ops=True,
    )

    if kind == "requested":
        if transfer.direction == "WASTAGE_PUSH":
            title = f"Incoming offer: {what} from {sender}"
            body = (
                f"{sender} is offering {what} that would otherwise expire. "
                f"They will authorise and book the courier; watch Transfers for the shipment."
            )
        else:
            title = f"Action needed: {receiver} requests {what}"
            body = (
                f"{receiver} is short and has asked {sender} for {what}"
                + (f" — reason: {transfer.reason}" if transfer.reason else "")
                + ". Open Transfers to authorise and book the courier, or decline."
            )
        notifications.notify(db, bank_ids=[counterparty], title=title, body=body, severity=severity, **common)
        if (transfer.priority or "").upper() == "EMERGENCY":
            _aws_alert(f"EMERGENCY platelet request {transfer.id}", f"{title}\n\n{body}")

    elif kind == "accepted":
        notifications.notify(
            db,
            bank_ids=[transfer.destination_bank_id],
            title=f"{sender} authorised {transfer.id}",
            body=(
                f"{what} reserved at {sender} (nearest expiry first) and a courier is booked"
                + (f" under AWB {transfer.awb_code}" if transfer.awb_code else "")
                + ". You will be told when the box is picked up."
            ),
            severity="success",
            **common,
        )

    elif kind == "declined":
        notifications.notify(
            db,
            bank_ids=[transfer.opened_by_bank_id],
            title=f"{sender} declined {transfer.id}",
            body=f"Reason: {transfer.decline_reason or 'none given'}. Try another facility from Transfers.",
            severity="warning",
            **common,
        )

    elif kind == "cancelled":
        notifications.notify(
            db,
            bank_ids=[counterparty],
            title=f"{transfer.id} was withdrawn",
            body=f"The opening facility cancelled the transfer of {what}. Any reserved units were released.",
            severity="info",
            **common,
        )

    elif kind == "dispatch_failed":
        title = f"Courier booking failed for {transfer.id}"
        body = f"{transfer.decline_reason or 'The courier rejected the booking.'} Units were returned to {sender}'s usable pool."
        notifications.notify(
            db,
            bank_ids=[transfer.source_bank_id, transfer.destination_bank_id],
            title=title,
            body=body,
            severity="critical",
            **common,
        )
        _aws_alert(title, body)

    elif kind == "picked_up":
        eta = transfer.eta_minutes or 0
        notifications.notify(
            db,
            bank_ids=[transfer.destination_bank_id],
            title=f"{what} on the way from {sender}",
            body=(
                f"Custody passed to {transfer.courier_name or 'the courier'}. Expected in about {round(eta)} min. "
                f"{sender} holds the receipt code and will pass it to you; you redeem it when the box arrives."
            ),
            severity="info",
            **common,
        )

    elif kind == "arrived":
        notifications.notify(
            db,
            bank_ids=[transfer.destination_bank_id],
            title=f"Courier has arrived with {transfer.id}",
            body="Check the cold-chain seal, then enter the sender's receipt code to settle the units onto your ledger.",
            severity="warning",
            **common,
        )

    elif kind == "completed":
        certificate_url = extra.get("certificate_url")
        data = {"details": details, "certificate_url": certificate_url}
        if certificate_url:
            data["details"] = details + [("Certificate", "Chain-of-custody PDF attached as link")]
        notifications.notify(
            db,
            bank_ids=[transfer.source_bank_id, transfer.destination_bank_id],
            event_type="TRANSFER_COMPLETED",
            title=f"{transfer.id} delivered and settled",
            body=(
                f"{receiver} redeemed the receipt code. {what} moved from {sender}'s ledger to {receiver}'s."
                + (" The chain-of-custody certificate is ready." if certificate_url else "")
            ),
            severity="success",
            transfer_id=transfer.id,
            link=certificate_url or notifications.console_link("transfers", transfer.id),
            data=data,
            ops=True,
        )


def _aws_alert(subject: str, message: str) -> None:
    def send():
        from cloud import aws

        aws.publish_alert(subject, message)

    notifications.background(send)


# ── Settlement follow-up: certificate, then the "completed" notification ────

def settle_followup(transfer_id: str) -> None:
    """
    Runs in the background after a delivery code is redeemed: render the
    custody certificate, store it, then notify both facilities with the link.
    """
    from services.certificate_service import issue_certificate

    db = SessionLocal()
    try:
        transfer = db.get(Transfer, transfer_id)
        if not transfer:
            return
        certificate_url = None
        try:
            certificate_url = issue_certificate(db, transfer)
        except Exception as exc:
            logger.warning(f"Certificate for {transfer_id} could not be stored: {exc!r}")
            capture_exception(exc)
        _notify(db, transfer, "completed", {"certificate_url": certificate_url})
    finally:
        db.close()
