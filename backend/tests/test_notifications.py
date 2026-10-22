"""
The notification pipeline end to end, with providers faked at the edge.
"""

import base64
import hashlib
import json
import time

import jwt
import pytest

from conftest import auth, sign_in
from services import notifications


def _run_transfer(client, sender_id, receiver_id, sender_token, receiver_token, units=2):
    created = client.post(
        "/api/v1/transfers",
        json={"counterparty_bank_id": sender_id, "direction": "SHORTAGE_PULL", "units": units, "component_type": "SDP"},
        headers=auth(receiver_token),
    )
    assert created.status_code == 200, created.text
    transfer_id = created.json()["data"]["id"]

    accepted = client.post(f"/api/v1/transfers/{transfer_id}/accept", headers=auth(sender_token))
    assert accepted.status_code == 200, accepted.text
    pickup = accepted.json()["data"]["pickup_otp_code"]

    picked = client.post(f"/api/v1/transfers/{transfer_id}/pickup/verify", json={"otp": pickup}, headers=auth(sender_token))
    assert picked.status_code == 200, picked.text
    receipt = picked.json()["data"]["delivery_otp_code"]

    settled = client.post(f"/api/v1/transfers/{transfer_id}/delivery/verify", json={"otp": receipt}, headers=auth(receiver_token))
    assert settled.status_code == 200, settled.text
    notifications.drain()
    return transfer_id, (pickup, receipt)


def _events(client, token):
    rows = client.get("/api/v1/notifications", headers=auth(token)).json()["data"]
    return [row["event_type"] for row in rows], rows


def test_transfer_lifecycle_notifies_the_right_facility(client, facilities):
    sender_id, receiver_id = facilities[0], facilities[1]
    sender = sign_in(client, "sender.officer@hospital.org", sender_id)
    receiver = sign_in(client, "receiver.officer@hospital.org", receiver_id)

    transfer_id, codes = _run_transfer(client, sender_id, receiver_id, sender, receiver)

    sender_events, _ = _events(client, sender)
    receiver_events, receiver_rows = _events(client, receiver)

    # The sender is asked to act; the receiver hears about each step after.
    assert "TRANSFER_REQUESTED" in sender_events
    assert "TRANSFER_REQUESTED" not in receiver_events
    for expected in ("TRANSFER_ACCEPTED", "TRANSFER_PICKED_UP", "TRANSFER_COMPLETED"):
        assert expected in receiver_events
    assert "TRANSFER_COMPLETED" in sender_events

    # No notification ever carries a custody code.
    _, sender_rows = _events(client, sender)
    everything = json.dumps(receiver_rows + sender_rows)
    for code in codes:
        assert code not in everything

    unread = client.get("/api/v1/notifications/unread-count", headers=auth(receiver)).json()["data"]["unread"]
    assert unread >= 3
    client.post("/api/v1/notifications/read-all", headers=auth(receiver))
    assert client.get("/api/v1/notifications/unread-count", headers=auth(receiver)).json()["data"]["unread"] == 0

    # Certificate renders on demand when no object storage is configured.
    cert = client.get(f"/api/v1/transfers/{transfer_id}/certificate", headers=auth(receiver))
    assert cert.status_code == 200
    assert cert.headers["content-type"] == "application/pdf"
    assert cert.content.startswith(b"%PDF")


def test_email_fails_over_to_the_next_provider(client, facilities, monkeypatch):
    from cloud import email as email_service

    calls = []

    def gmail_down(to, subject, html, text):
        calls.append("gmail")
        raise email_service.EmailError("Gmail API 503: backend error", retryable=True)

    def brevo_ok(to, subject, html, text):
        calls.append("brevo")
        return "brevo-message-1"

    monkeypatch.setenv("GMAIL_SENDER", "plateletiq.alerts@gmail.com")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("BREVO_API_KEY", "key")
    monkeypatch.setenv("BREVO_SENDER_EMAIL", "alerts@plateletiq.org")
    providers = dict(email_service.PROVIDERS)
    providers["gmail"] = (providers["gmail"][0], gmail_down, providers["gmail"][2])
    providers["brevo"] = (providers["brevo"][0], brevo_ok, providers["brevo"][2])
    monkeypatch.setattr(email_service, "PROVIDERS", providers)

    bank = facilities[2]
    token = sign_in(client, "duty.officer@realhospital.org", bank)
    res = client.post("/api/v1/notifications/test", headers=auth(token))
    assert res.status_code == 200, res.text
    notifications.drain()

    _, rows = _events(client, token)
    test_row = next(r for r in rows if r["event_type"] == "TEST")
    email = next(d for d in test_row["deliveries"] if d["channel"] == "email")
    assert email["status"] == "SENT"
    assert email["provider"] == "brevo"
    assert "gmail" in (email["error"] or "")
    assert email["target"].startswith("du***@")
    assert calls == ["gmail", "brevo"]


def test_undeliverable_dev_addresses_are_skipped():
    from cloud import email as email_service

    assert not email_service.is_deliverable("someone@hospital.test")
    assert not email_service.is_deliverable("x@example.com")
    assert email_service.is_deliverable("officer@gmail.com")


def _qstash_signature(key: str, url: str, body: bytes) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "Upstash",
            "sub": url,
            "exp": now + 300,
            "nbf": now - 5,
            "iat": now,
            "jti": "test",
            "body": base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("="),
        },
        key,
        algorithm="HS256",
    )


def test_queue_callback_requires_a_valid_qstash_signature(client, monkeypatch):
    monkeypatch.setenv("QSTASH_CURRENT_SIGNING_KEY", "sig_current_0123456789abcdef0123456")
    monkeypatch.setenv("QSTASH_NEXT_SIGNING_KEY", "sig_next_0123456789abcdef0123456789")
    body = json.dumps({"notification_id": "NTF-DOES-NOT-EXIST"}).encode()
    url = "https://plateletiq-api.onrender.com/api/v1/notifications/deliver"

    good = client.post(
        "/api/v1/notifications/deliver",
        content=body,
        headers={"Upstash-Signature": _qstash_signature("sig_next_0123456789abcdef0123456789", url, body), "Content-Type": "application/json"},
    )
    assert good.status_code == 200, good.text
    assert good.json()["status"] == "MISSING"

    tampered = client.post(
        "/api/v1/notifications/deliver",
        content=b'{"notification_id":"NTF-OTHER"}',
        headers={"Upstash-Signature": _qstash_signature("sig_current_0123456789abcdef0123456", url, body), "Content-Type": "application/json"},
    )
    assert tampered.status_code == 401

    unsigned = client.post("/api/v1/notifications/deliver", content=body)
    assert unsigned.status_code == 401


def test_jobs_need_a_secret_and_record_their_runs(client, facilities):
    assert client.post("/api/v1/jobs/expiry-sweep").status_code == 401
    res = client.post("/api/v1/jobs/expiry-sweep", headers={"X-Jobs-Secret": "test-jobs-secret"})
    assert res.status_code == 200, res.text
    run = res.json()["data"]
    assert run["status"] == "SUCCEEDED"
    assert "notifications_redriven" in run["summary"]

    digest = client.post("/api/v1/jobs/daily-digest", headers={"X-Jobs-Secret": "test-jobs-secret"})
    assert digest.status_code == 200, digest.text
    assert digest.json()["data"]["summary"]["facilities_sent"]

    token = sign_in(client, "jobs.viewer@hospital.org", facilities[0])
    listing = client.get("/api/v1/jobs", headers=auth(token)).json()["data"]
    names = {job["name"] for job in listing["jobs"]}
    assert names == {"expiry-sweep", "daily-digest", "forecast-refresh"}
    assert listing["recent_runs"]


def test_realtime_channel_auth_is_limited_to_own_facility(client, facilities, monkeypatch):
    from cloud import realtime

    monkeypatch.setenv("PUSHER_APP_ID", "1")
    monkeypatch.setenv("PUSHER_KEY", "public-key")
    monkeypatch.setenv("PUSHER_SECRET", "secret")
    monkeypatch.setenv("PUSHER_CLUSTER", "ap2")
    monkeypatch.setattr(realtime, "_client", None)

    mine, other = facilities[3], facilities[4]
    token = sign_in(client, "realtime.officer@hospital.org", mine)
    ok = client.post(
        "/api/v1/realtime/auth",
        content=f"socket_id=123.456&channel_name=private-facility-{mine}",
        headers={**auth(token), "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["auth"].startswith("public-key:")

    denied = client.post(
        "/api/v1/realtime/auth",
        content=f"socket_id=123.456&channel_name=private-facility-{other}",
        headers={**auth(token), "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert denied.status_code == 403
    monkeypatch.setattr(realtime, "_client", None)


def test_telegram_chat_links_through_the_webhook(client, facilities, monkeypatch):
    from cloud import telegram

    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "PlateletIQBot")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
    monkeypatch.setattr(telegram, "send_message", lambda chat, text, button=None: sent.append((chat, text)) or "1")

    bank = facilities[5]
    token = sign_in(client, "telegram.officer@hospital.org", bank)
    link = client.post("/api/v1/notifications/telegram/link", headers=auth(token)).json()["data"]
    assert link["deep_link"] == f"https://t.me/PlateletIQBot?start={link['code']}"

    update = {"update_id": 1, "message": {"text": f"/start {link['code']}", "chat": {"id": 9001}, "from": {"first_name": "Asha"}}}
    assert client.post("/api/v1/webhooks/telegram", json=update).status_code == 401
    res = client.post("/api/v1/webhooks/telegram", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"})
    assert res.status_code == 200
    assert "Linked" in sent[-1][1]

    status = client.get(f"/api/v1/notifications/telegram/links/{link['id']}", headers=auth(token)).json()["data"]
    assert status["status"] == "LINKED"

    stock = {"update_id": 2, "message": {"text": "/stock", "chat": {"id": 9001}}}
    client.post("/api/v1/webhooks/telegram", json=stock, headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"})
    assert "SDP" in sent[-1][1]

    settings = client.get("/api/v1/notifications/settings", headers=auth(token)).json()["data"]
    assert settings["telegram_chats"][0]["label"] == "Asha"


def test_custody_events_stream_to_sns_after_commit(client, facilities, monkeypatch):
    from cloud import aws

    published = []
    monkeypatch.setattr(aws, "sns_enabled", lambda: True)
    monkeypatch.setattr(aws, "publish_custody_event", lambda event: published.append(event) or "msg")

    sender = sign_in(client, "ledger.sender@hospital.org", facilities[6])
    receiver = sign_in(client, "ledger.receiver@hospital.org", facilities[7])
    transfer_id, _ = _run_transfer(client, facilities[6], facilities[7], sender, receiver, units=1)

    statuses = [e["status"] for e in published if e["transfer_id"] == transfer_id]
    assert statuses[0] == "REQUESTED"
    assert "IN_TRANSIT" in statuses and statuses[-1] == "TRANSFER_COMPLETED"
    # Officers are pseudonymised in the ledger.
    assert all("@" not in str(e.get("actor_ref") or "") for e in published)


def test_integrations_catalogue_reports_every_service(client, facilities):
    token = sign_in(client, "catalogue.viewer@hospital.org", facilities[0])
    data = client.get("/api/v1/system/integrations", headers=auth(token)).json()["data"]
    keys = {s["key"] for s in data["services"]}
    for expected in ("render", "supabase_postgres", "qstash", "pusher", "email_gmail", "telegram", "sentry", "aws_sns", "aws_lambda", "github"):
        assert expected in keys
    assert all(s["aws_equivalent"] for s in data["services"])

    db_test = client.post("/api/v1/system/test/supabase_postgres", headers=auth(token)).json()["data"]
    assert db_test["ok"] and "SELECT 1" in db_test["message"]


def test_qstash_dispatch_survives_an_instant_callback(client, facilities, monkeypatch):
    """QStash may call /deliver before publish() even returns; DELIVERED must stick."""
    from cloud import discord, qstash
    from database import SessionLocal
    from models.notification import Notification

    posted = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/test")
    monkeypatch.setattr(discord, "send", lambda *a, **k: posted.append(a[0]) or "discord-1")
    monkeypatch.setattr(qstash, "configured", lambda: True)

    def publish_and_call_back_immediately(path, body, **kwargs):
        assert path == notifications.DELIVER_PATH and kwargs["failure_path"] == notifications.DEAD_LETTER_PATH
        notifications.deliver(body["notification_id"])
        return "qstash-msg-1"

    monkeypatch.setattr(qstash, "publish", publish_and_call_back_immediately)

    token = sign_in(client, "queue.officer@hospital.org", facilities[8])
    created = client.post("/api/v1/notifications/test", headers=auth(token)).json()["data"][0]

    db = SessionLocal()
    row = db.get(Notification, created["id"])
    assert (row.dispatch_via, row.dispatch_status, row.queue_message_id) == ("qstash", "DELIVERED", "qstash-msg-1")
    db.close()
    assert posted == ["Test notification from PlateletIQ"]


def test_dead_letter_callback_marks_the_notification(client, facilities, monkeypatch):
    from database import SessionLocal
    from models.notification import Notification

    token = sign_in(client, "dlq.officer@hospital.org", facilities[9])
    created = client.post("/api/v1/notifications/test", headers=auth(token)).json()["data"][0]
    source = base64.b64encode(json.dumps({"notification_id": created["id"]}).encode()).decode()
    res = client.post(
        "/api/v1/notifications/dead-letter",
        json={"status": 500, "sourceBody": source},
        headers={"X-Jobs-Secret": "test-jobs-secret"},
    )
    assert res.json() == {"status": "recorded", "notification_id": created["id"]}
    db = SessionLocal()
    assert db.get(Notification, created["id"]).dispatch_status == "DEAD_LETTER"
    db.close()
