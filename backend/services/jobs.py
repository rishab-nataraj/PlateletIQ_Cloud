"""
Scheduled jobs.

Normally triggered by Upstash QStash schedules (see
``scripts/setup_qstash_schedules.py``). They can also be run by the
"Run a job" GitHub Actions workflow, or from the Cloud services screen, where
a manual run is scoped to the caller's own facility so a demo never spams the
other hospitals.

    expiry-sweep      hourly    retire expired units; warn each facility about
                                units expiring within 24 h (once a day);
                                remind senders about requests left waiting;
                                redrive notifications stuck in the outbox
    daily-digest      08:00 IST morning email + Telegram summary per facility,
                                and a network summary to the ops channel
    forecast-refresh  01:00 IST re-run the demand model for active facilities

Each run is recorded in ``job_runs`` and pings its heartbeat URL, so a job
that silently stops running raises an alert in Better Stack / Healthchecks.
"""

import datetime
import json
import uuid
from typing import Callable, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from cloud import cache, heartbeat
from cloud.observability import capture_exception, log_event, logger
from database import SessionLocal
from models.bank import Bank
from models.forecast import Forecast, ForecastRun, Recommendation
from models.inventory import InventoryUnit
from models.notification import JobRun
from models.transfer import TERMINAL_STATES, Transfer
from services import email_templates, notifications

IST = datetime.timedelta(hours=5, minutes=30)

# How long a request may sit unanswered before the sender gets a nudge.
REMINDER_AFTER = {"EMERGENCY": 15, "URGENT": 60, "ROUTINE": 360}  # minutes


def _today_ist() -> str:
    return (datetime.datetime.utcnow() + IST).date().isoformat()


def _scope(db: Session, only_bank_id: Optional[str]) -> list:
    if only_bank_id:
        return [only_bank_id]
    return notifications.facilities_with_members(db)


# ── expiry-sweep ─────────────────────────────────────────────────────────────

def expiry_sweep(db: Session, only_bank_id: Optional[str] = None, force: bool = False) -> dict:
    from services.facility_seed import mark_expired_units

    expired = mark_expired_units(db)
    now = datetime.datetime.utcnow()
    alerted, units_flagged, reminders = [], 0, 0

    for bank_id in _scope(db, only_bank_id):
        settings = notifications.get_settings(db, bank_id)
        if not settings.expiry_alerts_enabled:
            continue
        rows = (
            db.query(InventoryUnit.component_type, func.count(InventoryUnit.id))
            .filter(
                InventoryUnit.bank_id == bank_id,
                InventoryUnit.status == "AVAILABLE",
                InventoryUnit.expiry_at > now,
                InventoryUnit.expiry_at <= now + datetime.timedelta(hours=24),
            )
            .group_by(InventoryUnit.component_type)
            .all()
        )
        counts = {component: count for component, count in rows if count}
        total = sum(counts.values())
        if not total:
            continue
        # Once per facility per day, however often the sweep runs.
        if not force and not cache.set_once(f"expiry-alert:{bank_id}:{_today_ist()}", 26 * 3600):
            continue

        breakdown = ", ".join(f"{n} {c}" for c, n in sorted(counts.items()))
        notifications.notify(
            db,
            bank_ids=[bank_id],
            event_type="EXPIRY_ALERT",
            title=f"{total} platelet unit(s) expire within 24 hours",
            body=(
                f"{breakdown} will expire before this time tomorrow. Issue them first (FIFO), "
                "or offer them to a facility that is short with a Wastage push."
            ),
            severity="warning",
            link=notifications.console_link("transfers"),
            data={"details": [("Expiring < 24 h", breakdown)]},
        )
        alerted.append(bank_id)
        units_flagged += total

    # Requests nobody has answered.
    open_requests = db.query(Transfer).filter(Transfer.status == "REQUESTED").all()
    for transfer in open_requests:
        if only_bank_id and transfer.source_bank_id != only_bank_id:
            continue
        waited = (now - (transfer.created_at or now)).total_seconds() / 60
        if waited < REMINDER_AFTER.get((transfer.priority or "URGENT").upper(), 60):
            continue
        if not cache.set_once(f"reminder:{transfer.id}", 12 * 3600):
            continue
        receiver = db.get(Bank, transfer.destination_bank_id)
        notifications.notify(
            db,
            bank_ids=[transfer.source_bank_id],
            event_type="TRANSFER_REMINDER",
            title=f"Still waiting: {transfer.id} ({transfer.priority})",
            body=(
                f"{receiver.short_name if receiver else transfer.destination_bank_id} has waited "
                f"{round(waited)} min for {transfer.units} {transfer.component_type} unit(s). "
                "Authorise or decline so they can look elsewhere."
            ),
            severity="critical" if (transfer.priority or "").upper() == "EMERGENCY" else "warning",
            transfer_id=transfer.id,
            link=notifications.console_link("transfers", transfer.id),
            ops=True,
        )
        reminders += 1

    redriven = notifications.redrive(db)
    return {
        "expired_units_retired": expired,
        "facilities_alerted": alerted,
        "units_expiring_24h": units_flagged,
        "request_reminders": reminders,
        "notifications_redriven": redriven,
    }


# ── daily-digest ─────────────────────────────────────────────────────────────

def _facility_snapshot(db: Session, bank_id: str) -> dict:
    now = datetime.datetime.utcnow()
    stock = {}
    for component in ("SDP", "RDP"):
        base = db.query(func.count(InventoryUnit.id)).filter(
            InventoryUnit.bank_id == bank_id,
            InventoryUnit.component_type == component,
            InventoryUnit.status == "AVAILABLE",
            InventoryUnit.expiry_at > now,
        )
        stock[component] = {
            "available": base.scalar() or 0,
            "expiring_24h": base.filter(InventoryUnit.expiry_at <= now + datetime.timedelta(hours=24)).scalar() or 0,
            "expiring_48h": base.filter(InventoryUnit.expiry_at <= now + datetime.timedelta(hours=48)).scalar() or 0,
        }

    involved = db.query(Transfer).filter(
        (Transfer.source_bank_id == bank_id) | (Transfer.destination_bank_id == bank_id)
    )
    active = involved.filter(Transfer.status.notin_(list(TERMINAL_STATES))).all()
    completed_24h = involved.filter(
        Transfer.status == "TRANSFER_COMPLETED",
        Transfer.completed_at >= now - datetime.timedelta(hours=24),
    ).all()

    run = (
        db.query(ForecastRun)
        .filter(ForecastRun.bank_id == bank_id, ForecastRun.status == "SUCCEEDED")
        .order_by(ForecastRun.started_at.desc())
        .first()
    )
    forecast = []
    if run:
        forecast = (
            db.query(Forecast)
            .filter(Forecast.forecast_run_id == run.id)
            .order_by(Forecast.horizon_day.asc())
            .all()
        )
    recommendation = (
        db.query(Recommendation)
        .filter(Recommendation.bank_id == bank_id, Recommendation.status == "ACTIVE")
        .order_by(Recommendation.created_at.desc())
        .first()
    )
    return {
        "stock": stock,
        "active": active,
        "completed_24h": completed_24h,
        "forecast": forecast,
        "recommendation": recommendation,
    }


def daily_digest(db: Session, only_bank_id: Optional[str] = None, force: bool = False) -> dict:
    sent = []
    network = {"SDP": 0, "RDP": 0, "expiring": 0, "active": 0}
    today = _today_ist()

    for bank_id in _scope(db, only_bank_id):
        settings = notifications.get_settings(db, bank_id)
        if not settings.digest_enabled:
            continue
        bank = db.get(Bank, bank_id)
        if not bank:
            continue
        snap = _facility_snapshot(db, bank_id)
        stock = snap["stock"]
        expiring = stock["SDP"]["expiring_24h"] + stock["RDP"]["expiring_24h"]
        network["SDP"] += stock["SDP"]["available"]
        network["RDP"] += stock["RDP"]["available"]
        network["expiring"] += expiring
        network["active"] += len(snap["active"])

        rec = snap["recommendation"]
        week_q50 = round(sum(f.q50 for f in snap["forecast"]))
        week_q90 = round(sum(f.q90 for f in snap["forecast"]))

        paragraphs = [
            f"Good morning. Here is {bank.short_name or bank.name}'s platelet position for {today}.",
            (f"Recommendation: {rec.action}" + (f" {rec.quantity} units" if rec.quantity else "") + f". {rec.reason_summary}")
            if rec else "No active recommendation — run a forecast from Data setup to get one.",
        ]
        tables = [
            {
                "heading": "Stock",
                "columns": ["Component", "Usable", "Expire < 24 h", "Expire < 48 h"],
                "rows": [
                    [c, stock[c]["available"], stock[c]["expiring_24h"], stock[c]["expiring_48h"]]
                    for c in ("SDP", "RDP")
                ],
            },
            {
                "heading": "Transfers in progress",
                "columns": ["Transfer", "Units", "Counterparty", "Status"],
                "rows": [
                    [
                        t.id,
                        f"{t.units} {t.component_type}",
                        t.destination_bank_id if t.source_bank_id == bank_id else t.source_bank_id,
                        t.status.replace("_", " ").title(),
                    ]
                    for t in snap["active"]
                ],
                "empty": "No transfers in progress.",
            },
            {
                "heading": "Demand forecast (next 7 days)",
                "columns": ["Date", "Median (q50)", "High (q90)"],
                "rows": [[f.forecast_date.strftime("%a %d %b"), round(f.q50), round(f.q90)] for f in snap["forecast"]],
                "empty": "No forecast yet.",
            },
        ]
        html, text = email_templates.render(
            f"Daily platelet digest — {bank.short_name or bank.name}",
            paragraphs,
            details=[
                ("Usable SDP / RDP", f"{stock['SDP']['available']} / {stock['RDP']['available']}"),
                ("Expiring within 24 h", expiring),
                ("Transfers in progress", len(snap["active"])),
                ("Settled in last 24 h", len(snap["completed_24h"])),
                ("7-day demand (q50 / q90)", f"{week_q50} / {week_q90}" if snap["forecast"] else "—"),
            ],
            cta=("Open the console", notifications.console_link("overview")),
            severity="warning" if expiring else "info",
            facility_name=bank.name,
            tables=tables,
        )
        created = notifications.notify(
            db,
            bank_ids=[bank_id],
            event_type="DAILY_DIGEST",
            title=f"Daily digest: {stock['SDP']['available']} SDP, {stock['RDP']['available']} RDP usable",
            body=(
                f"{expiring} unit(s) expire within 24 h · {len(snap['active'])} transfer(s) in progress · "
                + (f"recommendation {rec.action}" if rec else "no active recommendation")
            ),
            severity="warning" if expiring else "info",
            link=notifications.console_link("overview"),
            data={"details": [("Expiring < 24 h", expiring), ("7-day demand q50", week_q50 if snap["forecast"] else "—")]},
            email_subject=f"[PlateletIQ] Daily digest {today} — {bank.short_name or bank.name}",
            email_html=html,
            dedupe_key=None if force else f"digest:{today}",
        )
        if created:
            sent.append(bank_id)

    if not only_bank_id and sent:
        notifications.notify(
            db,
            bank_ids=[None],
            event_type="NETWORK_DIGEST",
            title=f"Network digest {today}",
            body=(
                f"{len(sent)} active facilities · {network['SDP']} SDP and {network['RDP']} RDP usable · "
                f"{network['expiring']} unit(s) expiring within 24 h · {network['active']} transfer(s) in progress."
            ),
            severity="info",
            channels=(),
            ops=True,
            dedupe_key=None if force else f"network-digest:{today}",
        )
    return {"facilities_sent": sent, "network": network}


# ── forecast-refresh ─────────────────────────────────────────────────────────

def forecast_refresh(db: Session, only_bank_id: Optional[str] = None, force: bool = False) -> dict:
    from routers.forecast import trigger_forecast_run

    refreshed, failed = [], {}
    for bank_id in _scope(db, only_bank_id):
        try:
            trigger_forecast_run(bank_id=bank_id, db=db, current_user={"sub": "system-scheduler"})
            refreshed.append(bank_id)
        except Exception as exc:
            db.rollback()
            failed[bank_id] = str(exc)[:200]
    return {"refreshed": refreshed, "failed": failed}


JOBS: Dict[str, Callable] = {
    "expiry-sweep": expiry_sweep,
    "daily-digest": daily_digest,
    "forecast-refresh": forecast_refresh,
}

SCHEDULES = {
    # QStash cron runs in UTC. 08:00 IST = 02:30 UTC; 01:00 IST = 19:30 UTC.
    "expiry-sweep": {"cron": "5 * * * *", "description": "Hourly, at :05"},
    "daily-digest": {"cron": "30 2 * * *", "description": "Daily, 08:00 IST"},
    "forecast-refresh": {"cron": "30 19 * * *", "description": "Daily, 01:00 IST"},
}


def run_job(name: str, trigger: str, only_bank_id: Optional[str] = None, force: bool = False) -> dict:
    if name not in JOBS:
        raise KeyError(name)

    db = SessionLocal()
    run = JobRun(
        id=f"JOB-{uuid.uuid4().hex[:10].upper()}",
        job=name,
        trigger=trigger,
        scope=only_bank_id or "all",
        status="RUNNING",
        started_at=datetime.datetime.utcnow(),
    )
    db.add(run)
    db.commit()

    try:
        summary = JOBS[name](db, only_bank_id=only_bank_id, force=force)
        run.status = "SUCCEEDED"
        run.summary_json = json.dumps(summary, default=str)
        ok = True
    except Exception as exc:
        db.rollback()
        run = db.get(JobRun, run.id)
        run.status = "FAILED"
        run.error = f"{type(exc).__name__}: {exc}"[:1000]
        ok = False
        logger.exception(f"Job {name} failed: {exc!r}")
        capture_exception(exc)
    finally:
        run.finished_at = datetime.datetime.utcnow()
        db.commit()

    # Only a full scheduled run proves the schedule is alive.
    if not only_bank_id:
        heartbeat.ping(name, ok=ok)
    if not ok:
        try:
            from cloud import aws

            aws.publish_alert(f"PlateletIQ job {name} failed", run.error or "unknown error")
        except Exception:
            pass

    result = run.as_dict()
    log_event("job finished", job=name, trigger=trigger, status=run.status, scope=run.scope)
    db.close()
    return result
