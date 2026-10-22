"""
Error tracking (Sentry) and centralised logs (Better Stack).

Both are optional. Logs always go to stdout — which Render captures in its own
dashboard — and are additionally shipped to Better Stack when a source token
is set, so they survive restarts and can be searched across deploys.
"""

import json
import logging
import sys
import time

from cloud.config import app_env, env, env_bool, release

logger = logging.getLogger("plateletiq")

_state = {"sentry": False, "betterstack": False, "configured": False}


class _JsonFormatter(logging.Formatter):
    """One JSON object per line: easy to search in Render and Better Stack."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _ReadableFormatter(logging.Formatter):
    """Local development: a plain line, with any structured context appended."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extra = getattr(record, "context", None)
        if isinstance(extra, dict) and extra:
            line += " " + " ".join(f"{k}={v}" for k, v in extra.items())
        return line


def setup_logging() -> None:
    if _state["configured"]:
        return
    _state["configured"] = True

    logger.setLevel(logging.INFO)
    logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    if env_bool("LOG_JSON", default=app_env() != "development"):
        console.setFormatter(_JsonFormatter())
    else:
        console.setFormatter(_ReadableFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(console)

    token = env("BETTERSTACK_SOURCE_TOKEN")
    if token:
        try:
            from logtail import LogtailHandler

            host = env("BETTERSTACK_INGESTING_HOST") or "in.logs.betterstack.com"
            handler = LogtailHandler(source_token=token, host=host)
            handler.setLevel(logging.INFO)
            logger.addHandler(handler)
            _state["betterstack"] = True
            logger.info("Better Stack log shipping enabled", extra={"context": {"host": host}})
        except Exception as exc:  # never let logging setup take the API down
            logger.warning(f"Better Stack handler could not start: {exc!r}")


def init_sentry() -> None:
    dsn = env("SENTRY_DSN")
    if not dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            environment=app_env(),
            release=release(),
            traces_sample_rate=float(env("SENTRY_TRACES_SAMPLE_RATE") or 0.2),
            # Patient-adjacent system: no request bodies, cookies or IPs.
            send_default_pii=False,
        )
        _state["sentry"] = True
        logger.info("Sentry error tracking enabled")
    except Exception as exc:
        logger.warning(f"Sentry could not start: {exc!r}")


def capture_exception(exc: BaseException) -> None:
    """Report a handled exception without re-raising it."""
    if not _state["sentry"]:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def log_event(message: str, level: int = logging.INFO, **context) -> None:
    """Structured log line: ``log_event("notification sent", channel="email")``."""
    logger.log(level, message, extra={"context": context})


def sentry_status() -> dict:
    return {
        "configured": bool(env("SENTRY_DSN")),
        "active": _state["sentry"],
        "environment": app_env(),
    }


def betterstack_status() -> dict:
    return {
        "configured": bool(env("BETTERSTACK_SOURCE_TOKEN")),
        "active": _state["betterstack"],
    }
