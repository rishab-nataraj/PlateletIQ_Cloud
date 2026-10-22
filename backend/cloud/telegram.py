"""
Telegram Bot API — alerts on the duty officer's phone.

A facility links a chat by scanning a QR code on the Notifications screen.
The code opens ``t.me/<bot>?start=<one-time code>``; pressing *Start* sends
``/start <code>`` to the bot, Telegram forwards that to our webhook, and the
chat is bound to the facility. From then on every alert for that facility is
also a Telegram message, and the chat can ask the bot for ``/stock``.

Free, no card, no message quota worth worrying about at this scale (Telegram
asks bots to stay under ~30 messages/second).
"""

import html
import time
from typing import Optional

import httpx

from cloud.config import env

TIMEOUT = 10
_bot_username: dict = {}


class TelegramError(Exception):
    pass


def configured() -> bool:
    return bool(env("TELEGRAM_BOT_TOKEN"))


def _api(method: str, payload: Optional[dict] = None) -> dict:
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set.")
    res = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=payload or {}, timeout=TIMEOUT)
    body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
    if res.status_code != 200 or not body.get("ok"):
        raise TelegramError(f"Telegram {method} failed ({res.status_code}): {body.get('description') or res.text[:200]}")
    return body.get("result")


def bot_username() -> Optional[str]:
    """The bot's @name: from TELEGRAM_BOT_USERNAME, else asked of Telegram (cached)."""
    configured_name = env("TELEGRAM_BOT_USERNAME").lstrip("@")
    if configured_name:
        return configured_name
    if not configured():
        return None
    # Cache failures too, so a bad token cannot slow every status request.
    if time.time() - _bot_username.get("checked", 0) > 300 or "name" not in _bot_username:
        try:
            _bot_username["name"] = _api("getMe").get("username")
        except Exception:
            _bot_username.setdefault("name", None)
        _bot_username["checked"] = time.time()
    return _bot_username.get("name")


def deep_link(code: str) -> Optional[str]:
    name = bot_username()
    return f"https://t.me/{name}?start={code}" if name else None


def escape(text: str) -> str:
    return html.escape(text or "", quote=False)


def send_message(chat_id: str, text_html: str, button: Optional[tuple] = None) -> str:
    """Send an HTML-formatted message, optionally with one link button."""
    payload = {
        "chat_id": chat_id,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if button and button[1] and button[1].startswith("https://"):
        # Telegram refuses http:// and localhost URLs on buttons.
        payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
    result = _api("sendMessage", payload)
    return str(result.get("message_id", ""))


def get_updates(offset: Optional[int] = None) -> list:
    """Pending updates — only usable while no webhook is registered."""
    payload = {"timeout": 0, "allowed_updates": ["message"]}
    if offset is not None:
        payload["offset"] = offset
    return _api("getUpdates", payload) or []


def set_webhook(url: str, secret: str) -> dict:
    return _api(
        "setWebhook",
        {"url": url, "secret_token": secret, "allowed_updates": ["message"], "drop_pending_updates": True},
    )


def webhook_info() -> dict:
    return _api("getWebhookInfo")


def status() -> dict:
    return {
        "configured": configured(),
        "bot": bot_username() if configured() else None,
        "webhook_secret": bool(env("TELEGRAM_WEBHOOK_SECRET")),
    }
