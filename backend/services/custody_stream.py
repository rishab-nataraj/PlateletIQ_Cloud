"""
Stream every custody event to the AWS ledger.

Rather than remember to publish in each endpoint (and the courier webhook,
and any future code path), this listens to the SQLAlchemy session itself:

  * ``after_flush``  — note every new ``TransferEvent`` row being written.
  * ``after_commit`` — only once the transaction is durable, publish them to
                       the SNS custody topic, off the request thread.
  * ``after_rollback`` — forget them; an event that never committed never
                         happened.

So the ledger in DynamoDB can only ever contain events that also exist in the
application database, and it receives *all* of them.
"""

import datetime
import hashlib
from typing import List

from sqlalchemy import event

from cloud import aws
from cloud.config import app_env
from cloud.observability import capture_exception, logger
from models.transfer import TransferEvent

_KEY = "custody_events"
stats = {"published": 0, "failed": 0, "last_published_at": None, "last_error": None}


def _actor_ref(user_id) -> str:
    """Pseudonymise the officer: the ledger proves *a* member acted, not who."""
    if not user_id:
        return None
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:16]


def _serialise(row: TransferEvent) -> dict:
    return {
        "event_id": row.id,
        "transfer_id": row.transfer_id,
        "status": row.status,
        "title": row.title,
        "description": row.description,
        "actor_bank_id": row.actor_bank_id,
        "actor_ref": _actor_ref(row.actor_user_id),
        "source": row.source,
        "location_name": row.location_name,
        "lat": row.latitude,
        "lng": row.longitude,
        "occurred_at": (row.occurred_at or datetime.datetime.utcnow()).isoformat() + "Z",
        "environment": app_env(),
    }


def _publish(events: List[dict]) -> None:
    for item in events:
        try:
            aws.publish_custody_event(item)
            stats["published"] += 1
            stats["last_published_at"] = datetime.datetime.utcnow().isoformat()
        except Exception as exc:
            stats["failed"] += 1
            stats["last_error"] = str(exc)[:300]
            logger.warning(f"Custody event {item.get('event_id')} not published to SNS: {exc!r}")
            capture_exception(exc)


def install(session_factory) -> None:
    @event.listens_for(session_factory, "after_flush")
    def _capture(session, _context):
        rows = [obj for obj in session.new if isinstance(obj, TransferEvent)]
        if rows:
            session.info.setdefault(_KEY, []).extend(_serialise(r) for r in rows)

    @event.listens_for(session_factory, "after_commit")
    def _flush_to_sns(session):
        events = session.info.pop(_KEY, [])
        if events and aws.sns_enabled():
            from services.notifications import background

            background(_publish, events)

    @event.listens_for(session_factory, "after_rollback")
    def _discard(session):
        session.info.pop(_KEY, None)


def status() -> dict:
    return {"enabled": aws.sns_enabled(), **stats}
