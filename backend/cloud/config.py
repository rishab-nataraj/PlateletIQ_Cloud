"""Environment helpers shared by every cloud adapter."""

import os


def env(name: str, default: str = "") -> str:
    """A trimmed environment value. Placeholder values count as unset."""
    value = (os.getenv(name, default) or "").strip()
    if value.lower() in ("changeme", "your_value_here", "todo", "xxx"):
        return ""
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name) or default)
    except ValueError:
        return default


def env_list(name: str, default: str = "") -> list:
    return [item.strip() for item in (env(name) or default).split(",") if item.strip()]


def app_env() -> str:
    return env("APP_ENV", "development")


def public_api_url() -> str:
    """
    The address the outside world reaches this API on, e.g.
    ``https://plateletiq-api.onrender.com``.

    QStash and Telegram call *into* the API, so they need it. Render sets
    ``RENDER_EXTERNAL_URL`` on every web service, which covers the common case
    without any configuration.
    """
    return (env("PUBLIC_API_URL") or env("RENDER_EXTERNAL_URL")).rstrip("/")


def frontend_url() -> str:
    return env("FRONTEND_URL", "http://localhost:5173").rstrip("/")


def release() -> str:
    """The deployed commit, when the host tells us (Render, Vercel, GitHub)."""
    return env("RENDER_GIT_COMMIT") or env("GIT_COMMIT") or env("GITHUB_SHA") or "local"


def mask_email(address: str) -> str:
    """``rishab.nataraj@gmail.com`` -> ``ri***@gmail.com`` for delivery logs."""
    if not address or "@" not in address:
        return address or ""
    local, _, domain = address.partition("@")
    return f"{local[:2]}***@{domain}"
