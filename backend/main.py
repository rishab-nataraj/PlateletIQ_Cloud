import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import datetime
import time

# Observability first, so anything that fails during import is captured.
from cloud.observability import init_sentry, log_event, logger, setup_logging

setup_logging()
init_sentry()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from cloud import cache
from database import Base, SessionLocal, engine
from migrations import ensure_schema
from routers import (
    analytics_router,
    auth_router,
    camp_router,
    facilities_router,
    forecast_router,
    inventory_router,
    jobs_router,
    network_router,
    notifications_router,
    realtime_router,
    recommendation_router,
    requisitions_router,
    system_router,
    transfers_router,
    transport_router,
    webhooks_router,
)
from services import custody_stream

# Every committed TransferEvent is mirrored to the AWS custody ledger (SNS).
custody_stream.install(SessionLocal)

API_PREFIX = "/api/v1"

app = FastAPI(
    title="PlateletIQ API",
    description=(
        "Platelet inventory, demand forecasting, and inter-facility transfer logistics "
        "for eRaktKosh-registered blood centres."
    ),
    version="4.0.0",
)

# Uptime monitors identify themselves; remembering when each last checked in
# lets the Cloud services screen prove the monitor is really watching.
MONITOR_AGENTS = {"uptimerobot": "uptimerobot", "betterstack": "better", "render": "render"}


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    path = request.url.path
    if path.endswith("/health"):
        agent = request.headers.get("user-agent", "").lower()
        for name, marker in MONITOR_AGENTS.items():
            if marker in agent:
                cache.set_json(f"monitor:{name}:last", time.time(), ttl=7 * 24 * 3600)
    # Polling endpoints would drown the log; everything else is one JSON line.
    if not (request.method == "GET" and response.status_code < 400 and path.endswith(("/transfers", "/unread-count"))):
        log_event(
            "request",
            method=request.method,
            path=path,
            status=response.status_code,
            ms=elapsed_ms,
        )
    return response


def _allowed_origins() -> list:
    """
    Browser origins allowed to call this API.

    Two-laptop testing runs the UI from another machine's LAN address, so set
    ALLOWED_ORIGINS to include it, e.g.
        ALLOWED_ORIGINS=http://localhost:5173,http://192.168.1.42:5173
    ALLOWED_ORIGIN_REGEX covers a whole private range in one entry, e.g.
        ALLOWED_ORIGIN_REGEX=http://192\\.168\\.1\\.\\d{1,3}:5173
    """
    configured = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_origin_regex=os.getenv("ALLOWED_ORIGIN_REGEX") or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix=API_PREFIX)
app.include_router(facilities_router, prefix=API_PREFIX)
app.include_router(inventory_router, prefix=API_PREFIX)
app.include_router(forecast_router, prefix=API_PREFIX)
app.include_router(recommendation_router, prefix=API_PREFIX)
app.include_router(requisitions_router, prefix=API_PREFIX)
app.include_router(transfers_router, prefix=API_PREFIX)
app.include_router(network_router, prefix=API_PREFIX)
app.include_router(analytics_router, prefix=API_PREFIX)
app.include_router(camp_router, prefix=API_PREFIX)
app.include_router(transport_router, prefix=API_PREFIX)
app.include_router(webhooks_router, prefix=API_PREFIX)
app.include_router(notifications_router, prefix=API_PREFIX)
app.include_router(jobs_router, prefix=API_PREFIX)
app.include_router(system_router, prefix=API_PREFIX)
app.include_router(realtime_router, prefix=API_PREFIX)


@app.on_event("startup")
def startup() -> None:
    """Bring the schema up to date, then make sure the registry is populated."""
    Base.metadata.create_all(bind=engine)
    ensure_schema(engine)

    db = SessionLocal()
    try:
        from services.config_service import init_default_config
        from services.facility_seed import mark_expired_units, seed_all_inventory, seed_facilities

        facilities = seed_facilities(db)
        inventory = seed_all_inventory(db)
        expired = mark_expired_units(db)
        init_default_config(db)

        logger.info(
            f"Registry ready: {facilities['created']} facility/facilities created, "
            f"{facilities['refreshed']} refreshed; "
            f"{inventory['units_created']} unit(s) seeded across "
            f"{inventory['facilities_seeded']} facility/facilities; "
            f"{expired} expired unit(s) retired."
        )
    except Exception as exc:
        # A seeding failure must be visible, not silent — an empty registry
        # means the facility picker comes up blank.
        logger.exception(f"STARTUP ERROR while preparing the registry: {exc!r}")
        raise
    finally:
        db.close()


@app.on_event("shutdown")
def shutdown() -> None:
    """Let queued deliveries and ledger publishes finish before the process exits."""
    from services.notifications import drain

    drain(timeout=10)


@app.get(f"{API_PREFIX}/health")
def health_check():
    """Dependency health, plus which integrations are actually configured."""
    checks = {}

    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        from models.bank import Bank

        checks["database"] = "ok"
        checks["facilities_registered"] = db.query(Bank).count()
        db.close()
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        checks["facilities_registered"] = 0

    try:
        from routers.forecast import get_model

        get_model()
        checks["forecast_model"] = "ok"
    except Exception as exc:
        checks["forecast_model"] = f"unavailable: {exc}"

    transport_mode = os.getenv("TRANSPORT_MODE", "mock").lower()
    shiprocket_configured = bool(os.getenv("SHIPROCKET_EMAIL") and os.getenv("SHIPROCKET_PASSWORD"))

    from cloud import email as email_service, qstash, realtime, telegram, discord, aws, storage
    from cloud.observability import betterstack_status, sentry_status

    integrations = {
        # Says plainly whether courier calls hit Shiprocket or the simulator.
        "courier": "shiprocket_live" if (transport_mode == "live" and shiprocket_configured) else "simulated",
        "courier_webhook": "enabled" if os.getenv("SHIPROCKET_WEBHOOK_TOKEN") else "disabled",
        "routing": "mapbox" if os.getenv("MAPBOX_ACCESS_TOKEN") else "osrm_public",
        "google_signin": "enabled" if os.getenv("GOOGLE_CLIENT_ID") else "not_configured",
        "dev_signin": "enabled" if os.getenv("ALLOW_DEV_SIGNIN") == "1" else "disabled",
        "jwt_secret": "configured" if os.getenv("JWT_SECRET") else "ephemeral",
        "otp_secret": "configured" if os.getenv("OTP_SECRET") else "ephemeral",
        "database_engine": engine.dialect.name,
        "email": ",".join(email_service.provider_chain()) or "not_configured",
        "queue": "qstash" if qstash.configured() else "inline",
        "realtime": "pusher" if realtime.configured() else "polling",
        "telegram": "enabled" if telegram.configured() else "not_configured",
        "discord": "enabled" if discord.configured() else "not_configured",
        "cache": cache.backend_name(),
        "object_storage": ",".join(
            name for name, on in (("s3", aws.s3_enabled()), ("supabase", storage.configured())) if on
        ) or "not_configured",
        "aws_custody_ledger": "enabled" if aws.sns_enabled() else "not_configured",
        "sentry": "enabled" if sentry_status()["active"] else "not_configured",
        "betterstack_logs": "enabled" if betterstack_status()["active"] else "not_configured",
    }

    healthy = checks["database"] == "ok" and checks["facilities_registered"] > 0

    return {
        "status": "ok" if healthy else "degraded",
        "checks": checks,
        "integrations": integrations,
        "environment": os.getenv("APP_ENV", "development"),
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }
