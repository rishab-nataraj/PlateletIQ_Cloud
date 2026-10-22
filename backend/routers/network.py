from fastapi import APIRouter, Depends

from auth import get_current_user
from cloud import cache
from services.eraktkosh import load_district_summaries, load_network_summary

router = APIRouter(prefix="/network", tags=["network"])

# The eRaktKosh snapshot changes at most a few times a day, and scanning it is
# the slowest read in the API. Cache the computed view in Upstash Redis.
NETWORK_CACHE_SECONDS = 600


def _cached(key: str, loader):
    value = cache.get_json(key)
    if value is None:
        value = loader()
        cache.set_json(key, value, ttl=NETWORK_CACHE_SECONDS)
    return value


@router.get("")
def get_network(current_user: dict = Depends(get_current_user)):
    summary = _cached("network:summary", load_network_summary)
    districts = _cached("network:districts", load_district_summaries)

    return {
        "data": {
            "summary": summary,
            "districts": districts,
        },
        "error": None,
    }


@router.get("/risks")
def get_network_risks(current_user: dict = Depends(get_current_user)):
    districts = _cached("network:districts", load_district_summaries)
    # Filter out VERY_STALE districts for transfer suggestions per PRD AC-044
    viable = [d for d in districts if d.get("freshness_state") != "VERY_STALE"]
    surplus = [d for d in viable if d.get("sdp_units", 0) > 20]
    shortage = [d for d in viable if d.get("sdp_units", 0) == 0]

    return {
        "data": {
            "surplus_districts": surplus,
            "shortage_districts": shortage,
        },
        "error": None,
    }
