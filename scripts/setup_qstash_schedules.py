"""
Create (or update) the Upstash QStash cron schedules for PlateletIQ's jobs.

    cd backend
    python ../scripts/setup_qstash_schedules.py            # create / update
    python ../scripts/setup_qstash_schedules.py --list     # show what exists

Reads QSTASH_TOKEN and PUBLIC_API_URL from the environment or backend/.env
(``doppler run -- python ../scripts/setup_qstash_schedules.py`` works too).
Idempotent: each schedule has a fixed id, so re-running updates it in place.

    expiry-sweep      5 * * * *    hourly at :05 (UTC)
    daily-digest      30 2 * * *   08:00 IST
    forecast-refresh  30 19 * * *  01:00 IST
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, "backend", ".env"))

from cloud import qstash  # noqa: E402
from cloud.config import public_api_url  # noqa: E402
from services.jobs import SCHEDULES  # noqa: E402


def main() -> int:
    if not qstash.configured():
        print("QSTASH_TOKEN and PUBLIC_API_URL must both be set (PUBLIC_API_URL is your Render URL).")
        return 1

    if "--list" not in sys.argv:
        for job, spec in SCHEDULES.items():
            schedule_id = qstash.ensure_schedule(f"plateletiq-{job}", f"/api/v1/jobs/{job}", spec["cron"])
            print(f"  ✓ {job:<17} {spec['cron']:<13} {spec['description']}  (id {schedule_id})")
        print(f"\nQStash will POST to {public_api_url()}/api/v1/jobs/<job>, signed with your signing keys.")
        print("Make sure QSTASH_CURRENT_SIGNING_KEY and QSTASH_NEXT_SIGNING_KEY are set on the API.\n")

    print("Schedules in this QStash account:")
    for schedule in qstash.list_schedules():
        print(f"  {schedule['id']:<32} {schedule['cron']:<13} -> {schedule['destination']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
