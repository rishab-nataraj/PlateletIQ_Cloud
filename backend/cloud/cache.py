"""
Shared cache and coordination store: Upstash Redis over its REST API.

Used for three things the relational database is the wrong tool for:

  * **Caching** slow third-party answers — road routes from OSRM/Mapbox,
    the eRaktKosh network summary.
  * **Exactly-once guards** — "has today's expiry alert for this facility
    already gone out?", "is another worker already delivering notification X?"
    (``SET key NX EX ttl``).
  * **Rate limiting** — the demo "test this service" buttons, so a page left
    open on a projector cannot burn a free quota.

Upstash's REST interface suits a free Render instance: no persistent TCP
connection to keep alive across spin-downs. Without credentials the same calls
fall back to a per-process dictionary, which is correct for one instance and
says so on the Cloud services screen.
"""

import json
import threading
import time
from typing import Any, Optional

from cloud.config import env
from cloud.observability import logger

PREFIX = "plateletiq:"

_memory: dict = {}
_memory_lock = threading.Lock()
_client = None
_last_error: Optional[str] = None


def _redis():
    global _client
    if _client is not None:
        return _client
    url, token = env("UPSTASH_REDIS_REST_URL"), env("UPSTASH_REDIS_REST_TOKEN")
    if not (url and token):
        return None
    try:
        from upstash_redis import Redis

        _client = Redis(url=url, token=token, allow_telemetry=False)
    except Exception as exc:
        logger.warning(f"Upstash Redis client could not start: {exc!r}")
        _client = None
    return _client


def backend_name() -> str:
    return "upstash" if _redis() else "memory"


def _mem_get(key: str) -> Optional[str]:
    with _memory_lock:
        item = _memory.get(key)
        if not item:
            return None
        value, expires = item
        if expires and expires < time.time():
            _memory.pop(key, None)
            return None
        return value


def _mem_set(key: str, value: str, ttl: Optional[int], nx: bool = False) -> bool:
    with _memory_lock:
        current = _memory.get(key)
        if nx and current and (not current[1] or current[1] >= time.time()):
            return False
        _memory[key] = (value, time.time() + ttl if ttl else None)
        return True


def _fail(exc: Exception) -> None:
    global _last_error
    _last_error = f"{type(exc).__name__}: {exc}"
    logger.warning(f"Upstash Redis call failed, using in-process cache: {_last_error}")


def get_json(key: str) -> Any:
    full = PREFIX + key
    client = _redis()
    try:
        raw = client.get(full) if client else _mem_get(full)
    except Exception as exc:
        _fail(exc)
        raw = _mem_get(full)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def set_json(key: str, value: Any, ttl: Optional[int] = None) -> None:
    full, raw = PREFIX + key, json.dumps(value, default=str)
    client = _redis()
    try:
        if client:
            client.set(full, raw, ex=ttl)
            return
    except Exception as exc:
        _fail(exc)
    _mem_set(full, raw, ttl)


def set_once(key: str, ttl: int) -> bool:
    """True the first time ``key`` is claimed inside ``ttl`` seconds, else False."""
    full = PREFIX + key
    client = _redis()
    try:
        if client:
            return bool(client.set(full, "1", nx=True, ex=ttl))
    except Exception as exc:
        _fail(exc)
    return _mem_set(full, "1", ttl, nx=True)


def release(key: str) -> None:
    full = PREFIX + key
    client = _redis()
    try:
        if client:
            client.delete(full)
            return
    except Exception as exc:
        _fail(exc)
    with _memory_lock:
        _memory.pop(full, None)


def hit(key: str, window_seconds: int) -> int:
    """Count one hit in a fixed window and return the running total."""
    full = PREFIX + "rl:" + key
    client = _redis()
    try:
        if client:
            count = int(client.incr(full))
            if count == 1:
                client.expire(full, window_seconds)
            return count
    except Exception as exc:
        _fail(exc)
    with _memory_lock:
        value, expires = _memory.get(full, ("0", time.time() + window_seconds))
        if expires and expires < time.time():
            value, expires = "0", time.time() + window_seconds
        count = int(value) + 1
        _memory[full] = (str(count), expires)
        return count


def ping() -> dict:
    """Round trip to the store, for the integrations screen."""
    started = time.perf_counter()
    client = _redis()
    if not client:
        return {"ok": True, "backend": "memory", "latency_ms": 0}
    try:
        client.set(PREFIX + "ping", str(time.time()), ex=60)
        client.get(PREFIX + "ping")
        return {
            "ok": True,
            "backend": "upstash",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as exc:
        _fail(exc)
        return {"ok": False, "backend": "upstash", "error": _last_error}


def status() -> dict:
    return {
        "configured": bool(env("UPSTASH_REDIS_REST_URL") and env("UPSTASH_REDIS_REST_TOKEN")),
        "backend": backend_name(),
        "last_error": _last_error,
    }
