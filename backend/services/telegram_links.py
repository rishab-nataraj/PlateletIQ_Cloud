"""
Telegram chat linking and the bot's small command set.

    /start <code>   bind this chat to the facility that generated the code
    /stock          live usable stock for the linked facility
    /transfers      transfers in progress
    /stop           unlink this chat
    /help           what the bot does

Used by both the webhook (production) and the "check now" polling fallback
(local development, where Telegram cannot reach localhost).
"""

import datetime
import secrets
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from cloud import telegram
from models.bank import Bank
from models.inventory import InventoryUnit
from models.notification import TelegramLink
from models.transfer import TERMINAL_STATES, Transfer

CODE_TTL = datetime.timedelta(minutes=30)

HELP = (
    "<b>PlateletIQ alerts</b>\n"
    "This chat receives transfer and expiry alerts for the facility it is linked to.\n\n"
    "/stock — usable platelets right now\n"
    "/transfers — transfers in progress\n"
    "/stop — stop alerts in this chat\n\n"
    "Custody codes are never sent here."
)


def new_code() -> str:
    return "piq" + secrets.token_hex(6)


def _chat_label(message: dict) -> str:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("title"):
        return chat["title"]
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
    handle = f"@{sender['username']}" if sender.get("username") else ""
    return (f"{name} {handle}".strip()) or f"chat {chat.get('id')}"


def _linked_bank(db: Session, chat_id: str) -> Optional[Bank]:
    link = (
        db.query(TelegramLink)
        .filter(TelegramLink.chat_id == chat_id, TelegramLink.status == "LINKED")
        .order_by(TelegramLink.linked_at.desc())
        .first()
    )
    return db.get(Bank, link.bank_id) if link else None


def stock_text(db: Session, bank: Bank) -> str:
    now = datetime.datetime.utcnow()
    lines = [f"<b>{telegram.escape(bank.short_name or bank.name)}</b> — usable platelets"]
    for component in ("SDP", "RDP"):
        base = db.query(func.count(InventoryUnit.id)).filter(
            InventoryUnit.bank_id == bank.id,
            InventoryUnit.component_type == component,
            InventoryUnit.status == "AVAILABLE",
            InventoryUnit.expiry_at > now,
        )
        total = base.scalar() or 0
        soon = base.filter(InventoryUnit.expiry_at <= now + datetime.timedelta(hours=24)).scalar() or 0
        lines.append(f"{component}: <b>{total}</b> ({soon} expire within 24 h)")
    return "\n".join(lines)


def transfers_text(db: Session, bank: Bank) -> str:
    rows = (
        db.query(Transfer)
        .filter(
            (Transfer.source_bank_id == bank.id) | (Transfer.destination_bank_id == bank.id),
            Transfer.status.notin_(list(TERMINAL_STATES)),
        )
        .order_by(Transfer.updated_at.desc())
        .limit(8)
        .all()
    )
    if not rows:
        return "No transfers in progress."
    lines = ["<b>Transfers in progress</b>"]
    for t in rows:
        arrow = "→" if t.source_bank_id == bank.id else "←"
        other = t.destination_bank_id if t.source_bank_id == bank.id else t.source_bank_id
        lines.append(f"{t.id} {arrow} {other}: {t.units} {t.component_type}, {t.status.replace('_', ' ').lower()}")
    return "\n".join(lines)


def handle_message(db: Session, message: dict) -> Optional[str]:
    """Process one incoming message. Returns the reply text (HTML) or None."""
    text = (message.get("text") or "").strip()
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not text or not chat_id:
        return None
    command, _, argument = text.partition(" ")
    command = command.split("@")[0].lower()

    if command == "/start":
        code = argument.strip()
        if not code:
            bank = _linked_bank(db, chat_id)
            return (f"This chat is linked to {telegram.escape(bank.name)}.\n\n" + HELP) if bank else (
                "Open <b>Notifications → Telegram</b> in the PlateletIQ console and scan the QR code to link this chat."
            )
        link = db.query(TelegramLink).filter(TelegramLink.link_code == code).first()
        if not link or link.status == "REVOKED":
            return "That link code is not valid. Generate a new one from the PlateletIQ console."
        if link.status == "PENDING" and link.created_at and datetime.datetime.utcnow() - link.created_at > CODE_TTL:
            return "That link code has expired. Generate a new one from the PlateletIQ console."
        if link.status == "LINKED" and link.chat_id != chat_id:
            return "That link code was already used by another chat."
        link.chat_id = chat_id
        link.chat_label = _chat_label(message)
        link.status = "LINKED"
        link.linked_at = link.linked_at or datetime.datetime.utcnow()
        db.commit()
        bank = db.get(Bank, link.bank_id)
        return (
            f"✅ Linked to <b>{telegram.escape(bank.name if bank else link.bank_id)}</b>.\n"
            "You will get transfer and expiry alerts here.\n\n" + HELP
        )

    bank = _linked_bank(db, chat_id)
    if command == "/help":
        return HELP
    if not bank:
        return "This chat is not linked to a facility yet. Scan the QR code under Notifications → Telegram."
    if command == "/stock":
        return stock_text(db, bank)
    if command == "/transfers":
        return transfers_text(db, bank)
    if command == "/stop":
        for link in db.query(TelegramLink).filter(TelegramLink.chat_id == chat_id, TelegramLink.status == "LINKED"):
            link.status = "REVOKED"
        db.commit()
        return "Alerts stopped for this chat. Link it again from the console any time."
    return HELP


def process_update(db: Session, update: dict) -> Optional[str]:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None
    reply = handle_message(db, message)
    if reply:
        telegram.send_message(str(message["chat"]["id"]), reply)
    return reply


def poll_updates(db: Session) -> int:
    """
    Fallback when no webhook is registered (local development): read pending
    updates directly, answer them, then acknowledge them so they are not
    returned again. Returns how many were processed.
    """
    updates = telegram.get_updates()
    for update in updates:
        try:
            process_update(db, update)
        except Exception:
            db.rollback()
    if updates:
        telegram.get_updates(offset=max(u["update_id"] for u in updates) + 1)
    return len(updates)
