"""
Pusher private-channel authorisation.

pusher-js POSTs ``socket_id`` and ``channel_name`` (form-encoded) here before
subscribing. We sign only ``private-facility-<the caller's own bank id>``.
"""

from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import require_facility
from cloud import realtime

router = APIRouter(prefix="/realtime", tags=["realtime"])


@router.post("/auth")
async def authorise_channel(request: Request, current_user: dict = Depends(require_facility)):
    raw = (await request.body()).decode("utf-8")
    if request.headers.get("content-type", "").startswith("application/json"):
        import json

        fields = json.loads(raw or "{}")
    else:
        fields = {k: v[0] for k, v in parse_qs(raw).items()}

    socket_id, channel = fields.get("socket_id"), fields.get("channel_name")
    if not socket_id or not channel:
        raise HTTPException(status_code=400, detail="socket_id and channel_name are required.")
    try:
        return realtime.authorise(channel, socket_id, current_user.get("bank_id"))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get("/config")
def realtime_config():
    """Public: the browser's Pusher key and cluster (both are public values)."""
    return {"data": realtime.client_config(), "error": None}
