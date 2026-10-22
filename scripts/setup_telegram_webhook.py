"""
Point your Telegram bot at the deployed API.

    cd backend
    python ../scripts/setup_telegram_webhook.py           # register the webhook
    python ../scripts/setup_telegram_webhook.py --info    # show current state
    python ../scripts/setup_telegram_webhook.py --delete  # back to polling (local dev)

Needs TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET and PUBLIC_API_URL (env or
backend/.env). Telegram then POSTs every message to
``PUBLIC_API_URL/api/v1/webhooks/telegram`` with the secret in the
``X-Telegram-Bot-Api-Secret-Token`` header, which the API checks.

Locally there is no public URL: leave TELEGRAM_WEBHOOK_SECRET empty, run
--delete once, and the console's "Link a Telegram chat" button polls instead.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, "backend", ".env"))

from cloud import telegram  # noqa: E402
from cloud.config import env, public_api_url  # noqa: E402


def main() -> int:
    if not telegram.configured():
        print("TELEGRAM_BOT_TOKEN is not set.")
        return 1

    if "--delete" in sys.argv:
        telegram._api("deleteWebhook", {"drop_pending_updates": False})
        print("Webhook removed. The API will poll for /start messages instead.")
        return 0

    if "--info" not in sys.argv:
        secret, base = env("TELEGRAM_WEBHOOK_SECRET"), public_api_url()
        if not (secret and base):
            print("Set TELEGRAM_WEBHOOK_SECRET (any long random string) and PUBLIC_API_URL first.")
            return 1
        telegram.set_webhook(f"{base}/api/v1/webhooks/telegram", secret)
        print(f"Webhook registered for @{telegram.bot_username()} -> {base}/api/v1/webhooks/telegram")

    info = telegram.webhook_info()
    print(json.dumps({k: info.get(k) for k in ("url", "pending_update_count", "last_error_message")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
