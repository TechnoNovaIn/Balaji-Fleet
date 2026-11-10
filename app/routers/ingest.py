"""Ingest endpoints — poll, backfill, sync-vehicles, resolve-odometer, reload-models, compute-trips."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import require_admin
from app.db import connect
from app.ingest import backfill, poll, sync_vehicles
from app.intangles import IntanglesClient
from app.ml_tonnage import get_models
from app.odometer import recompute_odometer, resolve_odometer_sources
from app.tonnage_regression import get_regressor
from app.trips import compute_all as compute_all_trips

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])


class BackfillRequest(BaseModel):
    days: int = 30
    vehicles: Optional[list[str]] = None


class PollRequest(BaseModel):
    vehicles: Optional[list[str]] = None


@router.post("/poll")
def trigger_poll(req: PollRequest, user: dict = Depends(require_admin)):
    client = IntanglesClient()
    with connect() as conn:
        totals = poll(client, conn, req.vehicles)
    return totals


@router.post("/backfill")
def trigger_backfill(req: BackfillRequest, user: dict = Depends(require_admin)):
    client = IntanglesClient()
    with connect() as conn:
        totals = backfill(client, conn, req.days, req.vehicles)
    return totals


@router.post("/sync-vehicles")
def trigger_sync(user: dict = Depends(require_admin)):
    client = IntanglesClient()
    with connect() as conn:
        n = sync_vehicles(client, conn)
    return {"synced": n}


@router.post("/resolve-odometer")
def trigger_resolve(user: dict = Depends(require_admin)):
    with connect() as conn:
        resolved = resolve_odometer_sources(conn)
        updated = recompute_odometer(conn)
        conn.commit()
    return {**resolved, "telemetry_updated": updated}


@router.post("/reload-models")
def trigger_reload_models(user: dict = Depends(require_admin)):
    """Reload the trained classifier + calibration artifacts from disk.

    The train loop writes new models/ files; this picks them up in-process
    without restarting the API.
    """
    classifier, calibration = get_models(refresh=True)
    regressor = get_regressor(refresh=True)
    fam = {}
    if calibration:
        fam = calibration.get("families", {})
        fam["global"] = calibration.get("global")
    return {
        "reloaded": True,
        "classifier_loaded": classifier is not None,
        "payload_regressor_loaded": regressor is not None,
        "families": fam,
    }


@router.post("/compute-trips")
def trigger_compute_trips(req: BackfillRequest, user: dict = Depends(require_admin)):
    """Segment + label + store trips for recent days (admin).

    Idempotent: each vehicle-day is recomputed (DELETE+INSERT), so re-running
    with more days only adds coverage. Equivalent to `python -m scripts.trips`.
    """
    total = compute_all_trips(req.days, req.vehicles)
    return {"computed_trips": total, "days": req.days}
