"""
Pusher Channels — live events pushed to open consoles.

The transfer screens used to learn about the other facility's actions only by
polling every 2.5 seconds. With Pusher configured, the API publishes an event
the moment something changes and every console of that facility reacts at
once: a toast, a browser notification, and a refetch. Polling stays on as a
slow safety net (20 s) rather than the primary mechanism.

Each facility has a **private** channel, ``private-facility-<bank id>``.
Pusher only lets a browser subscribe after our ``/realtime/auth`` endpoint
signs the request, and that endpoint checks the caller's JWT is bound to that
facility — so one hospital cannot listen to another's channel.
"""

from typing import Optional

from cloud.config import env

_client = None


def configured() -> bool:
    return bool(env("PUSHER_APP_ID") and env("PUSHER_KEY") and env("PUSHER_SECRET") and env("PUSHER_CLUSTER"))


def _pusher():
    global _client
    if _client is None and configured():
        import pusher

        _client = pusher.Pusher(
            app_id=env("PUSHER_APP_ID"),
            key=env("PUSHER_KEY"),
            secret=env("PUSHER_SECRET"),
            cluster=env("PUSHER_CLUSTER"),
            ssl=True,
            timeout=6,
        )
    return _client


def facility_channel(bank_id: str) -> str:
    return f"private-facility-{bank_id}"


def publish(bank_id: str, event: str, data: dict) -> bool:
    """Push ``event`` to every console signed in at ``bank_id``."""
    client = _pusher()
    if not client:
        return False
    client.trigger(facility_channel(bank_id), event, data)
    return True


def authorise(channel: str, socket_id: str, bank_id: Optional[str]) -> dict:
    """Sign a private-channel subscription, but only for the caller's facility."""
    client = _pusher()
    if not client:
        raise PermissionError("Realtime is not configured on this server.")
    if not bank_id or channel != facility_channel(bank_id):
        raise PermissionError("You can only subscribe to your own facility's channel.")
    return client.authenticate(channel=channel, socket_id=socket_id)


def client_config() -> dict:
    """What the browser needs to connect. The key and cluster are public."""
    if not configured():
        return {"enabled": False}
    return {"enabled": True, "key": env("PUSHER_KEY"), "cluster": env("PUSHER_CLUSTER")}


def status() -> dict:
    return {"configured": configured(), "cluster": env("PUSHER_CLUSTER") or None}
