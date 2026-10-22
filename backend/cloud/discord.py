"""
Discord incoming webhook — the network operations channel.

Facility alerts go to that facility's people. This channel is different: it
sees every transfer across the network, the way a regional blood transfusion
officer or a control room would. One webhook URL, no bot, no OAuth.
"""

import datetime
from typing import List, Optional

import httpx

from cloud.config import env

SEVERITY_COLOURS = {"info": 0x0071E3, "success": 0x1A8A2C, "warning": 0xB25000, "critical": 0xC41230}


class DiscordError(Exception):
    pass


def configured() -> bool:
    return env("DISCORD_WEBHOOK_URL").startswith("https://")


def send(title: str, description: str, severity: str = "info", fields: Optional[List[dict]] = None,
         url: Optional[str] = None) -> str:
    webhook = env("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise DiscordError("DISCORD_WEBHOOK_URL is not set.")

    embed = {
        "title": title[:256],
        "description": description[:4000],
        "color": SEVERITY_COLOURS.get(severity, SEVERITY_COLOURS["info"]),
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "footer": {"text": "PlateletIQ network operations"},
        "fields": [
            {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", True)}
            for f in (fields or [])
        ][:10],
    }
    if url and url.startswith("https://"):
        embed["url"] = url

    # ?wait=true makes Discord return the created message, so we can log its id.
    res = httpx.post(f"{webhook}?wait=true", json={"username": "PlateletIQ", "embeds": [embed]}, timeout=10)
    if res.status_code >= 300:
        raise DiscordError(f"Discord webhook {res.status_code}: {res.text[:200]}")
    try:
        return str(res.json().get("id", ""))
    except ValueError:
        return ""


def status() -> dict:
    return {"configured": configured()}
