"""
Scheduled job endpoints.

``POST /jobs/{name}``           QStash schedule / GitHub Actions / curl with JOBS_SECRET.
                                 Runs across every active facility.
``POST /jobs/{name}/run-mine``  a signed-in officer, from the Cloud services
                                 screen. Runs for that officer's facility only.
``GET  /jobs``                  schedules and recent runs, for the console.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from auth import require_facility
from cloud import cache, heartbeat, qstash
from database import get_db
from models.notification import JobRun
from routers.internal_auth import verify_internal
from services import jobs

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
def list_jobs(db: Session = Depends(get_db), current_user: dict = Depends(require_facility)):
    recent = db.query(JobRun).order_by(JobRun.started_at.desc()).limit(30).all()
    schedules = []
    try:
        schedules = qstash.list_schedules() if qstash.configured() else []
    except Exception:
        schedules = []
    return {
        "data": {
            "jobs": [
                {
                    "name": name,
                    "cron_utc": spec["cron"],
                    "description": spec["description"],
                    "heartbeat": bool(heartbeat.url_for(name)),
                    "scheduled_in_qstash": any(s["destination"].endswith(f"/jobs/{name}") for s in schedules),
                    "last_run": next((r.as_dict() for r in recent if r.job == name), None),
                }
                for name, spec in jobs.SCHEDULES.items()
            ],
            "recent_runs": [r.as_dict() for r in recent],
            "scheduler": "qstash" if schedules else ("qstash (no schedules yet)" if qstash.configured() else "not configured"),
        },
        "error": None,
    }


@router.post("/{name}")
async def run_scheduled(name: str, request: Request):
    auth = await verify_internal(request)
    if name not in jobs.JOBS:
        raise HTTPException(status_code=404, detail=f"Unknown job '{name}'.")
    trigger = "qstash" if auth["via"] == "qstash" else auth["via"]
    result = jobs.run_job(name, trigger=trigger)
    if result["status"] == "FAILED":
        # Non-2xx lets QStash retry the schedule invocation.
        raise HTTPException(status_code=500, detail=result.get("error") or "Job failed.")
    return {"data": result, "error": None}


@router.post("/{name}/run-mine")
def run_for_my_facility(name: str, current_user: dict = Depends(require_facility)):
    if name not in jobs.JOBS:
        raise HTTPException(status_code=404, detail=f"Unknown job '{name}'.")
    if cache.hit(f"job-manual:{current_user['bank_id']}:{name}", 60) > 2:
        raise HTTPException(status_code=429, detail="This job ran twice in the last minute. Give it a moment.")
    result = jobs.run_job(name, trigger="manual", only_bank_id=current_user["bank_id"], force=True)
    return {"data": result, "error": None}
