"""
Notifications, their per-channel deliveries, and where each facility wants them.

One ``Notification`` is one message to one facility (it is also that
facility's in-app inbox entry). Each way it leaves the building — an email to
one officer, a Telegram message to one linked chat, the Discord ops channel,
the realtime push — is a ``NotificationDelivery`` with its own status,
provider and error, so the delivery log can show exactly what happened.
"""

import datetime
import json

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from database import Base

NOTIFICATION_STATES = ["PENDING", "QUEUED", "DELIVERED", "PARTIAL", "FAILED", "DEAD_LETTER"]
DELIVERY_STATES = ["PENDING", "SENT", "FAILED", "SKIPPED"]


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(String, primary_key=True)
    bank_id = Column(String, ForeignKey("banks.id"), nullable=True, index=True)
    event_type = Column(String, nullable=False, index=True)
    severity = Column(String, default="info")  # info | success | warning | critical
    title = Column(String, nullable=False)
    body = Column(Text, nullable=True)
    transfer_id = Column(String, nullable=True, index=True)
    link = Column(String, nullable=True)  # deep link into the console
    data_json = Column(Text, nullable=True)

    email_subject = Column(String, nullable=True)
    email_html = Column(Text, nullable=True)

    dispatch_via = Column(String, nullable=True)  # qstash | inline
    dispatch_status = Column(String, default="PENDING", index=True)
    queue_message_id = Column(String, nullable=True)
    dedupe_key = Column(String, nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    delivered_at = Column(DateTime, nullable=True)
    read_at = Column(DateTime, nullable=True)

    @property
    def data(self) -> dict:
        try:
            return json.loads(self.data_json) if self.data_json else {}
        except (TypeError, ValueError):
            return {}


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    id = Column(String, primary_key=True)
    notification_id = Column(String, ForeignKey("notifications.id"), nullable=False, index=True)
    channel = Column(String, nullable=False)  # email | telegram | discord | realtime
    target = Column(String, nullable=True)  # address / chat id / channel name
    target_label = Column(String, nullable=True)  # masked, safe to show
    status = Column(String, default="PENDING")
    provider = Column(String, nullable=True)  # gmail | brevo | resend | ses | telegram | ...
    provider_message_id = Column(String, nullable=True)
    attempts = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)


class FacilityNotificationSettings(Base):
    __tablename__ = "facility_notification_settings"

    bank_id = Column(String, ForeignKey("banks.id"), primary_key=True)
    email_enabled = Column(Boolean, default=True)
    telegram_enabled = Column(Boolean, default=True)
    digest_enabled = Column(Boolean, default=True)
    expiry_alerts_enabled = Column(Boolean, default=True)
    # Extra recipients beyond the facility's signed-in members, comma separated.
    extra_emails = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)

    @property
    def extra_email_list(self) -> list:
        return [e.strip().lower() for e in (self.extra_emails or "").split(",") if e.strip()]

    def as_dict(self) -> dict:
        return {
            "bank_id": self.bank_id,
            "email_enabled": bool(self.email_enabled),
            "telegram_enabled": bool(self.telegram_enabled),
            "digest_enabled": bool(self.digest_enabled),
            "expiry_alerts_enabled": bool(self.expiry_alerts_enabled),
            "extra_emails": self.extra_email_list,
        }


class TelegramLink(Base):
    """A Telegram chat bound to a facility through a one-time /start code."""

    __tablename__ = "telegram_links"

    id = Column(String, primary_key=True)
    bank_id = Column(String, ForeignKey("banks.id"), nullable=False, index=True)
    link_code = Column(String, nullable=False, unique=True, index=True)
    chat_id = Column(String, nullable=True, index=True)
    chat_label = Column(String, nullable=True)
    status = Column(String, default="PENDING")  # PENDING | LINKED | REVOKED
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    linked_at = Column(DateTime, nullable=True)


class JobRun(Base):
    """One execution of a scheduled job, whoever triggered it."""

    __tablename__ = "job_runs"

    id = Column(String, primary_key=True)
    job = Column(String, nullable=False, index=True)
    trigger = Column(String, nullable=False)  # qstash | github | manual | cli
    scope = Column(String, nullable=True)  # all | a bank id
    status = Column(String, default="RUNNING")  # RUNNING | SUCCEEDED | FAILED
    started_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    finished_at = Column(DateTime, nullable=True)
    summary_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)

    def as_dict(self) -> dict:
        try:
            summary = json.loads(self.summary_json) if self.summary_json else None
        except (TypeError, ValueError):
            summary = None
        return {
            "id": self.id,
            "job": self.job,
            "trigger": self.trigger,
            "scope": self.scope,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": (
                round((self.finished_at - self.started_at).total_seconds() * 1000)
                if self.finished_at and self.started_at else None
            ),
            "summary": summary,
            "error": self.error,
        }
