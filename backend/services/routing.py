"""
Road routing for transfer legs.

Two providers, tried in order:

  1. **Mapbox Directions** (``driving-traffic``) when ``MAPBOX_ACCESS_TOKEN``
     is set — gives traffic-aware ETAs.
  2. **OSRM** (``router.project-osrm.org``) — the public demo server for the
     OpenStreetMap routing engine. No key, so live road geometry works out of
     the box.

If both are unreachable we fall back to a straight-line Haversine estimate so
a transfer is never blocked by a routing outage; the response says which
provider produced it, and the UI labels an estimated route as such.
"""

import math
import os
from typing import Any, Dict, List, Tuple

import requests

MAPBOX_ACCESS_TOKEN = os.getenv("MAPBOX_ACCESS_TOKEN", "").strip()
MAPBOX_DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox/driving-traffic"
OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")

# Chennai city driving average, used only by the Haversine fallback.
FALLBACK_SPEED_KMPH = float(os.getenv("FALLBACK_SPEED_KMPH", "22"))
FALLBACK_OVERHEAD_MIN = float(os.getenv("FALLBACK_OVERHEAD_MIN", "6"))


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_km = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return round(radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 3)


def _straight_line_route(o_lat: float, o_lng: float, d_lat: float, d_lng: float) -> Dict[str, Any]:
    distance_km = haversine_km(o_lat, o_lng, d_lat, d_lng)
    duration_min = round((distance_km / FALLBACK_SPEED_KMPH) * 60 + FALLBACK_OVERHEAD_MIN, 1)
    return {
        "distance_km": distance_km,
        "duration_min": duration_min,
        "route_geometry": {
            "type": "LineString",
            "coordinates": [[o_lng, o_lat], [d_lng, d_lat]],
        },
        "provider": "haversine_estimate",
        "is_estimate": True,
    }


def _mapbox_route(o_lat: float, o_lng: float, d_lat: float, d_lng: float) -> Dict[str, Any]:
    url = f"{MAPBOX_DIRECTIONS_URL}/{o_lng},{o_lat};{d_lng},{d_lat}"
    res = requests.get(
        url,
        params={
            "access_token": MAPBOX_ACCESS_TOKEN,
            "geometries": "geojson",
            "overview": "full",
            "steps": "false",
        },
        timeout=6,
    )
    res.raise_for_status()
    routes = res.json().get("routes") or []
    if not routes:
        raise ValueError("Mapbox returned no route")
    route = routes[0]
    return {
        "distance_km": round(route["distance"] / 1000.0, 3),
        "duration_min": round(route["duration"] / 60.0, 1),
        "route_geometry": route["geometry"],
        "provider": "mapbox_directions",
        "is_estimate": False,
    }


def _osrm_route(o_lat: float, o_lng: float, d_lat: float, d_lng: float) -> Dict[str, Any]:
    url = f"{OSRM_BASE_URL}/route/v1/driving/{o_lng},{o_lat};{d_lng},{d_lat}"
    res = requests.get(
        url,
        params={"overview": "full", "geometries": "geojson", "alternatives": "false", "steps": "false"},
        timeout=8,
    )
    res.raise_for_status()
    payload = res.json()
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise ValueError(f"OSRM returned {payload.get('code')}")
    route = payload["routes"][0]
    return {
        "distance_km": round(route["distance"] / 1000.0, 3),
        "duration_min": round(route["duration"] / 60.0, 1),
        "route_geometry": route["geometry"],
        "provider": "osrm",
        "is_estimate": False,
    }


ROUTE_CACHE_SECONDS = int(os.getenv("ROUTE_CACHE_SECONDS", "21600"))  # 6 h


def get_route(o_lat: float, o_lng: float, d_lat: float, d_lng: float, use_cache: bool = True) -> Dict[str, Any]:
    """
    Best available road route between two points.

    Real routes are cached in Upstash Redis for a few hours, keyed by the two
    coordinates: the facility pairs in a city repeat constantly, and the
    public OSRM server is a shared resource we should not hammer. A
    straight-line fallback is never cached, so the next request retries the
    real providers.
    """
    key = f"route:{o_lat:.5f},{o_lng:.5f}:{d_lat:.5f},{d_lng:.5f}:{'mb' if MAPBOX_ACCESS_TOKEN else 'osrm'}"
    if use_cache:
        try:
            from cloud import cache

            cached = cache.get_json(key)
            if cached:
                return {**cached, "cached": True}
        except Exception:
            pass

    route = None
    if MAPBOX_ACCESS_TOKEN:
        try:
            route = _mapbox_route(o_lat, o_lng, d_lat, d_lng)
        except Exception as exc:
            print(f"Mapbox directions unavailable, trying OSRM: {exc}")

    if route is None:
        try:
            route = _osrm_route(o_lat, o_lng, d_lat, d_lng)
        except Exception as exc:
            print(f"OSRM unavailable, using straight-line estimate: {exc}")

    if route is None:
        return _straight_line_route(o_lat, o_lng, d_lat, d_lng)

    try:
        from cloud import cache

        cache.set_json(key, route, ttl=ROUTE_CACHE_SECONDS)
    except Exception:
        pass
    return route


# ── Geometry helpers used by the live tracking view ──────────────────────────

def _coords(route_geometry: Dict[str, Any]) -> List[Tuple[float, float]]:
    """GeoJSON [lng, lat] pairs as (lat, lng) tuples."""
    if not route_geometry:
        return []
    return [(pt[1], pt[0]) for pt in route_geometry.get("coordinates", []) if len(pt) >= 2]


def cumulative_lengths(route_geometry: Dict[str, Any]) -> Tuple[List[Tuple[float, float]], List[float]]:
    """Vertices plus the running distance in km at each vertex."""
    pts = _coords(route_geometry)
    running = [0.0]
    for i in range(1, len(pts)):
        running.append(running[-1] + haversine_km(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
    return pts, running


def point_at_fraction(route_geometry: Dict[str, Any], fraction: float) -> Dict[str, float]:
    """
    Interpolate a position ``fraction`` (0..1) of the way along the route,
    measured by distance rather than by vertex index so the speed is even.
    """
    pts, running = cumulative_lengths(route_geometry)
    if not pts:
        return {}
    if len(pts) == 1:
        return {"lat": pts[0][0], "lng": pts[0][1]}

    fraction = max(0.0, min(1.0, fraction))
    total = running[-1]
    if total <= 0:
        return {"lat": pts[-1][0], "lng": pts[-1][1]}

    target = total * fraction
    for i in range(1, len(running)):
        if running[i] >= target:
            span = running[i] - running[i - 1]
            local = 0.0 if span <= 0 else (target - running[i - 1]) / span
            lat = pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * local
            lng = pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * local
            return {"lat": round(lat, 6), "lng": round(lng, 6)}

    return {"lat": pts[-1][0], "lng": pts[-1][1]}
