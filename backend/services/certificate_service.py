"""
Issue and fetch chain-of-custody certificates.

Storage preference: Amazon S3 (from the AWS stack) → Supabase Storage. With
neither configured the certificate is still available — it is rendered on
demand by ``GET /transfers/{id}/certificate`` — it just is not archived.
"""

from typing import Optional, Tuple

from sqlalchemy.orm import Session

from cloud import aws, storage
from cloud.certificates import build_certificate, fingerprint, object_key
from models.bank import Bank
from models.inventory import InventoryUnit
from models.transfer import Transfer, TransferEvent


def render(db: Session, transfer: Transfer) -> Tuple[bytes, str]:
    """Returns ``(pdf_bytes, sha256_of_timeline)``."""
    source = db.get(Bank, transfer.source_bank_id)
    destination = db.get(Bank, transfer.destination_bank_id)
    events = (
        db.query(TransferEvent)
        .filter(TransferEvent.transfer_id == transfer.id)
        .order_by(TransferEvent.occurred_at.asc())
        .all()
    )
    unit_ids = transfer.reserved_unit_ids
    bags = [
        row.bag_id
        for row in db.query(InventoryUnit.bag_id).filter(InventoryUnit.id.in_(unit_ids)).all()
    ] if unit_ids else []
    pdf = build_certificate(transfer, source, destination, events, bags)
    digest = fingerprint(
        [
            {
                "status": e.status,
                "title": e.title,
                "actor": e.actor_bank_id,
                "at": e.occurred_at.isoformat() if e.occurred_at else None,
            }
            for e in events
        ]
    )
    return pdf, digest


def issue_certificate(db: Session, transfer: Transfer) -> Optional[str]:
    """Render, store, and record the certificate. Returns a shareable link or None."""
    pdf, digest = render(db, transfer)
    key = object_key(transfer.id)

    if aws.s3_enabled():
        stored = aws.put_object(key, pdf, "application/pdf")
        transfer.certificate_store = "s3"
    elif storage.configured():
        stored = storage.upload(key, pdf, "application/pdf")
        transfer.certificate_store = "supabase"
    else:
        transfer.certificate_sha256 = digest
        db.commit()
        return None

    transfer.certificate_key = key
    transfer.certificate_sha256 = digest
    db.commit()
    return stored.get("url")


def fresh_link(transfer: Transfer) -> Optional[str]:
    """A new short-lived link to an archived certificate."""
    if not transfer.certificate_key:
        return None
    if transfer.certificate_store == "s3" and aws.s3_enabled():
        return aws.presign(transfer.certificate_key, seconds=15 * 60)
    if transfer.certificate_store == "supabase" and storage.configured():
        return storage.signed_url(transfer.certificate_key, seconds=15 * 60)
    return None
