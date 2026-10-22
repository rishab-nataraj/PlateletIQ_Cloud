"""
Test harness: a throwaway SQLite database and the real FastAPI app.

No cloud credentials are used. Each test that needs a provider switches it on
with environment variables and replaces the network call with a fake, so the
suite exercises our wiring (what gets sent, to whom, retried how) without
sending anything.
"""

import os
import sys
import tempfile

import pytest

_db_dir = tempfile.mkdtemp(prefix="plateletiq-tests-")
# TEST_DATABASE_URL runs the suite against PostgreSQL (as CI does); default SQLite.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{os.path.join(_db_dir, 'test.db')}"
os.environ["ALLOW_DEV_SIGNIN"] = "1"
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["OTP_SECRET"] = "test-otp-secret"
os.environ["JOBS_SECRET"] = "test-jobs-secret"
os.environ["APP_ENV"] = "test"
os.environ["GEOCODE_FACILITIES"] = "0"
# Keep tests off the network: an unreachable OSRM falls back to straight lines.
os.environ["OSRM_BASE_URL"] = "http://127.0.0.1:9"

for name in list(os.environ):
    if name.startswith(("PUSHER_", "QSTASH_", "TELEGRAM_", "BREVO_", "RESEND_", "GMAIL_", "UPSTASH_", "AWS_", "SUPABASE_", "SENTRY_", "BETTERSTACK_", "DISCORD_")):
        del os.environ[name]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def facilities(client):
    token = sign_in(client, "registry.reader@hospital.org")
    rows = client.get("/api/v1/facilities", headers=auth(token)).json()["data"]
    return [row["id"] for row in rows]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def sign_in(client, email: str, facility_id: str = None) -> str:
    res = client.post("/api/v1/auth/dev-signin", json={"email": email})
    assert res.status_code == 200, res.text
    token = res.json()["data"]["token"]
    if facility_id:
        res = client.post(
            "/api/v1/auth/select-facility",
            json={"facility_id": facility_id, "role": "OFFICER"},
            headers=auth(token),
        )
        assert res.status_code == 200, res.text
        token = res.json()["data"]["token"]
    return token
