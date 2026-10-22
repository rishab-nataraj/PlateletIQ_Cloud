"""
Transactional email with provider failover.

Four providers, all on free tiers, all over HTTPS. That last part matters:
Render's free instances block outbound SMTP (ports 25/465/587), so classic
``smtplib`` cannot work there no matter which mail service is behind it.

    gmail   Gmail API (Google Cloud). Sends *as* your own Gmail address, so
            Google signs it and it lands in the inbox. Needs a one-time OAuth
            refresh token (scripts/gmail_refresh_token.py).
    brevo   Brevo transactional API. 300 emails/day to any recipient. Without
            your own domain Brevo rewrites a Gmail sender, so mail may land in
            spam — fine for a demo, check the spam folder.
    resend  Resend API. Without a verified domain it only delivers to the
            address that owns the Resend account.
    ses     Amazon SES (from the AWS stack). In the SES sandbox both sender
            and recipient must be verified identities.

``EMAIL_PROVIDERS`` sets the order (default ``gmail,brevo,resend,ses``); only
configured providers take part. Each send tries them in order and stops at the
first success, so one provider's outage or exhausted quota falls through to
the next — the response records every attempt for the delivery log.
"""

import base64
import time
from email.message import EmailMessage
from typing import Dict, List, Optional

import httpx

from cloud.config import env, env_list

DEFAULT_ORDER = "gmail,brevo,resend,ses"
TIMEOUT = 12

# Addresses that can never receive mail. Dev sign-in accepts anything with an
# "@", so test accounts like officer@hospital.test would otherwise bounce.
UNDELIVERABLE_SUFFIXES = (".test", ".example", ".invalid", ".localhost", ".local", "@example.com", "@example.org")


class EmailError(Exception):
    def __init__(self, message: str, attempts: Optional[list] = None, retryable: bool = True):
        super().__init__(message)
        self.attempts = attempts or []
        self.retryable = retryable


def from_name() -> str:
    return env("EMAIL_FROM_NAME", "PlateletIQ Alerts")


# ── Recipient policy ─────────────────────────────────────────────────────────

def is_deliverable(address: str) -> bool:
    """
    Whether we should mail ``address`` at all.

    ``EMAIL_ALLOWLIST`` (comma-separated addresses or ``@domain`` entries)
    restricts delivery during a demo, so a stray dev account cannot receive
    alerts. The registry's real hospital addresses are never used as
    recipients anywhere; only signed-in members and addresses a facility adds
    on the Notifications screen are.
    """
    address = (address or "").strip().lower()
    if "@" not in address or address.endswith(UNDELIVERABLE_SUFFIXES):
        return False
    allow = [entry.lower() for entry in env_list("EMAIL_ALLOWLIST")]
    if not allow:
        return True
    domain = "@" + address.split("@", 1)[1]
    return address in allow or domain in allow


# ── Providers ────────────────────────────────────────────────────────────────

_gmail_token: Dict[str, float] = {}


def _gmail_configured() -> bool:
    return bool(
        env("GMAIL_SENDER")
        and env("GMAIL_REFRESH_TOKEN")
        and (env("GMAIL_CLIENT_ID") or env("GOOGLE_CLIENT_ID"))
        and (env("GMAIL_CLIENT_SECRET") or env("GOOGLE_CLIENT_SECRET"))
    )


def _gmail_access_token() -> str:
    token, expires = _gmail_token.get("token"), _gmail_token.get("expires", 0)
    if token and expires > time.time() + 60:
        return token
    res = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": env("GMAIL_CLIENT_ID") or env("GOOGLE_CLIENT_ID"),
            "client_secret": env("GMAIL_CLIENT_SECRET") or env("GOOGLE_CLIENT_SECRET"),
            "refresh_token": env("GMAIL_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        },
        timeout=TIMEOUT,
    )
    if res.status_code != 200:
        raise EmailError(f"Gmail OAuth refresh failed ({res.status_code}): {res.text[:200]}", retryable=False)
    body = res.json()
    _gmail_token["token"] = body["access_token"]
    _gmail_token["expires"] = time.time() + int(body.get("expires_in", 3600))
    return body["access_token"]


def _send_gmail(to: List[str], subject: str, html: str, text: str) -> str:
    message = EmailMessage()
    message["From"] = f"{from_name()} <{env('GMAIL_SENDER')}>"
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(text or "This message needs an HTML-capable mail client.")
    message.add_alternative(html, subtype="html")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    res = httpx.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {_gmail_access_token()}"},
        json={"raw": raw},
        timeout=TIMEOUT,
    )
    if res.status_code >= 300:
        raise EmailError(f"Gmail API {res.status_code}: {res.text[:200]}", retryable=res.status_code >= 500)
    return res.json().get("id", "")


def _send_brevo(to: List[str], subject: str, html: str, text: str) -> str:
    res = httpx.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": env("BREVO_API_KEY"), "accept": "application/json"},
        json={
            "sender": {"name": from_name(), "email": env("BREVO_SENDER_EMAIL")},
            "to": [{"email": address} for address in to],
            "subject": subject,
            "htmlContent": html,
            "textContent": text or None,
            "tags": ["plateletiq"],
        },
        timeout=TIMEOUT,
    )
    if res.status_code >= 300:
        raise EmailError(f"Brevo {res.status_code}: {res.text[:200]}", retryable=res.status_code >= 500 or res.status_code == 429)
    return res.json().get("messageId", "")


def _send_resend(to: List[str], subject: str, html: str, text: str) -> str:
    sender = env("RESEND_FROM") or f"{from_name()} <onboarding@resend.dev>"
    res = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {env('RESEND_API_KEY')}"},
        json={"from": sender, "to": to, "subject": subject, "html": html, "text": text or None},
        timeout=TIMEOUT,
    )
    if res.status_code >= 300:
        raise EmailError(f"Resend {res.status_code}: {res.text[:200]}", retryable=res.status_code >= 500 or res.status_code == 429)
    return res.json().get("id", "")


def _send_ses(to: List[str], subject: str, html: str, text: str) -> str:
    from cloud import aws

    return aws.ses_send(to, subject, html, text, sender=f"{from_name()} <{env('AWS_SES_SENDER')}>")


PROVIDERS = {
    "gmail": (_gmail_configured, _send_gmail, "Gmail API (Google Cloud)"),
    "brevo": (lambda: bool(env("BREVO_API_KEY") and env("BREVO_SENDER_EMAIL")), _send_brevo, "Brevo"),
    "resend": (lambda: bool(env("RESEND_API_KEY")), _send_resend, "Resend"),
    "ses": (lambda: bool(env("AWS_SES_SENDER") and env("AWS_REGION")), _send_ses, "Amazon SES"),
}


def provider_chain() -> List[str]:
    order = env_list("EMAIL_PROVIDERS", DEFAULT_ORDER)
    return [name for name in order if name in PROVIDERS and PROVIDERS[name][0]()]


def send_email(to: List[str], subject: str, html: str, text: str = "") -> dict:
    """
    Send through the first provider that accepts the message.

    Returns ``{"provider", "message_id", "attempts"}``; raises ``EmailError``
    carrying every attempt when all configured providers fail.
    """
    recipients = [address for address in to if is_deliverable(address)]
    if not recipients:
        raise EmailError("No deliverable recipients.", retryable=False)

    chain = provider_chain()
    if not chain:
        raise EmailError("No email provider is configured.", retryable=False)

    attempts, retryable = [], False
    for name in chain:
        sender = PROVIDERS[name][1]
        started = time.perf_counter()
        try:
            message_id = sender(recipients, subject, html, text)
            attempts.append({"provider": name, "ok": True, "ms": round((time.perf_counter() - started) * 1000)})
            return {"provider": name, "message_id": message_id, "attempts": attempts, "recipients": recipients}
        except Exception as exc:
            # Network errors and 5xx/429 may clear up on a retry; a rejected
            # API key will not, so the queue should not keep hammering it.
            retryable = retryable or getattr(exc, "retryable", True)
            attempts.append({"provider": name, "ok": False, "error": str(exc)[:300]})

    raise EmailError(
        "All email providers failed: " + "; ".join(f"{a['provider']}: {a.get('error')}" for a in attempts),
        attempts=attempts,
        retryable=retryable,
    )


def status() -> dict:
    return {
        "chain": provider_chain(),
        "providers": {
            name: {"label": label, "configured": check()} for name, (check, _, label) in PROVIDERS.items()
        },
        "allowlist": env_list("EMAIL_ALLOWLIST"),
    }
