"""FastAPI application for Balaji Fleet Intangles Ingest & Management."""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.auth import router as auth_router
from app.config import settings
from app.db import close_pool, connect, init_schema
from app.routers import drivers_router, ingest_router, telemetry_router, vehicles_router

log = logging.getLogger(__name__)


def _schema_ready() -> bool:
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT (to_regclass('public.telemetry') IS NOT NULL AND "
                    "to_regclass('public.trips') IS NOT NULL) AS ready"
                )
                row = cur.fetchone()
                return bool(row and row["ready"])
    except Exception as e:
        log.warning("Schema check failed: %s", e)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: validate config & ensure DB schema
    settings.validate_jwt_secret()
    if not _schema_ready():
        log.info("Schema not initialized. Running init_schema()...")
        init_schema()
    else:
        log.info("Database schema is ready.")
    yield
    # Shutdown: gracefully close the connection pool
    log.info("Shutting down. Closing database connection pool...")
    close_pool()


app = FastAPI(title="Balaji Fleet Intangles Ingest", lifespan=lifespan)

# CORS middleware using origins from settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_store(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


# Include modular routers
app.include_router(auth_router)
app.include_router(vehicles_router)
app.include_router(telemetry_router)
app.include_router(ingest_router)
app.include_router(drivers_router)


@app.get("/health")
def health():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM vehicles")
            vehicles = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM telemetry")
            telemetry = cur.fetchone()["n"]
            cur.execute("SELECT max(time) AS last FROM telemetry")
            last = cur.fetchone()["last"]
    return {
        "status": "ok",
        "vehicles": vehicles,
        "telemetry_rows": telemetry,
        "last_telemetry": last,
    }
